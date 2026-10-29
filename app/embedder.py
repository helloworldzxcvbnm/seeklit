"""向量化层（可插拔）。

设计目标：**默认零下载、零外部服务**即可跑通语义检索；
需要更高质量时，改一个环境变量即可切到神经向量模型。

后端：
  lsa                   默认。jieba/字符分词 → TF-IDF → 随机化截断 SVD（LSA）
                        纯 numpy 实现，完全离线，维度由 EMBED_DIM 决定。
  ollama                调用本机 Ollama /api/embed（例如 bge-m3，1024 维）
  sentence_transformers 调用本地/HF 的 sentence-transformers 模型
  openai                任意 OpenAI 兼容 /v1/embeddings 接口

所有后端输出统一为 float32、L2 归一化的二维数组，配合 seekdb 的
VECTOR 列 + DISTANCE=COSINE + HNSW 索引使用。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import urllib.error
import urllib.request
from typing import Iterable, Sequence

import numpy as np

from . import config

log = logging.getLogger("seeklit.embed")

try:  # jieba 可选：缺失时退化为中文字符二元组，仍能工作
    import jieba  # type: ignore

    jieba.setLogLevel(logging.ERROR)
    _HAS_JIEBA = True
except Exception:  # noqa: BLE001
    _HAS_JIEBA = False

# 中英文混排分词：中文按字/词，英文与代码标识符按 token，数字单独保留
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{1,}")
_NUM_TOKEN = re.compile(r"(?<![\w])\d+(?:\.\d+)?(?![\w])")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_STOP = {
    "的", "了", "和", "是", "在", "与", "及", "或", "对", "为", "以", "中", "上", "下",
    "并", "被", "把", "从", "到", "也", "就", "都", "而", "且", "这", "那", "其", "之",
    "一个", "我们", "可以", "进行", "通过", "如果", "因为", "所以", "但是", "以及",
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "be",
    "with", "as", "by", "at", "from", "that", "this", "it", "we", "can", "which",
}


def tokenize(text: str) -> list[str]:
    """把一段文本切成检索/向量化的基本词元。

    - 中文：优先 jieba 精确模式；无 jieba 时退化为字符二元组（bigram）
    - 英文/代码标识符：整体保留并小写（CUSUM、Page-Hinkley、min_run 这类术语不能拆）
    - 数字：单独保留（阈值、参数值是关键检索线索，例如 L=3、ARL0=500）
    """
    if not text:
        return []
    tokens: list[str] = []

    for m in _ASCII_TOKEN.finditer(text):
        tok = m.group(0).lower()
        if tok not in _STOP:
            tokens.append(tok)
    for m in _NUM_TOKEN.finditer(text):
        tokens.append(m.group(0))

    for run in _CJK_RUN.findall(text):
        if _HAS_JIEBA:
            for w in jieba.cut(run):
                w = w.strip()
                if len(w) >= 2 and w not in _STOP:
                    tokens.append(w)
            # 追加字级 bigram，缓解分词粒度不一致导致的漏召回
            if len(run) >= 2:
                for i in range(len(run) - 1):
                    bg = run[i : i + 2]
                    if bg not in _STOP:
                        tokens.append(bg)
        else:
            if len(run) >= 2:
                for i in range(len(run) - 1):
                    tokens.append(run[i : i + 2])
            else:
                tokens.append(run)
    return tokens


# ---------------------------------------------------------------------------
# 后端实现
# ---------------------------------------------------------------------------
class BaseEmbedder:
    name = "base"
    dim: int = 0
    requires_fit = False
    fitted = False

    def fit(self, texts: Sequence[str]) -> "BaseEmbedder":  # pragma: no cover
        return self

    def encode(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    def describe(self) -> dict:
        return {"backend": self.name, "dim": self.dim, "fitted": self.fitted}


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


class LSAEmbedder(BaseEmbedder):
    """离线 TF-IDF + 随机化截断 SVD。

    这是经典 LSA（Latent Semantic Analysis）：在语料上拟合词项共现的低秩结构，
    使「不同措辞、相同主题」的段落获得相近向量。它不依赖任何预训练权重，
    因此在无外网带宽的机器上也能得到可用的语义检索能力。

    数学形式：
        X  = tfidf(D)                     # n_docs x n_terms，行 L2 归一化
        X ≈ U Σ Vᵀ  （取前 k 个奇异三元组）
        vec(d) = normalize(x_d · V_k)     # 文档/查询同一条投影，保证可比
    """

    name = "lsa"
    requires_fit = True

    def __init__(self, dim: int | None = None, min_df: int | None = None,
                 max_features: int | None = None, model_path=None, seed: int = 42) -> None:
        self.dim = int(dim or config.EMBED_DIM)
        self.min_df = int(min_df if min_df is not None else config.LSA_MIN_DF)
        self.max_features = int(max_features or config.LSA_MAX_FEATURES)
        self.model_path = model_path or config.LSA_MODEL_PATH
        self.seed = seed
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray | None = None
        self.components: np.ndarray | None = None  # k x n_terms
        self.singular: np.ndarray | None = None
        self.n_docs_fit: int = 0
        self.fitted = False

    # -- 词表与 TF-IDF ------------------------------------------------------
    def _build_vocab(self, token_lists: list[list[str]]) -> None:
        df: dict[str, int] = {}
        for toks in token_lists:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        # 先按 min_df 过滤，再按文档频次取前 max_features
        items = [(t, c) for t, c in df.items() if c >= self.min_df]
        items.sort(key=lambda kv: (-kv[1], kv[0]))
        items = items[: self.max_features]
        self.vocab = {t: i for i, (t, _) in enumerate(items)}
        n = max(len(token_lists), 1)
        self.idf = np.zeros(len(self.vocab), dtype=np.float32)
        for t, c in items:
            # 平滑 IDF，与 scikit-learn 的 smooth_idf 一致
            self.idf[self.vocab[t]] = math.log((1.0 + n) / (1.0 + c)) + 1.0
        log.info("LSA vocab built: %d terms (min_df=%d, max_features=%d)",
                 len(self.vocab), self.min_df, self.max_features)

    def _tfidf_matrix(self, token_lists: Iterable[Sequence[str]]) -> np.ndarray:
        assert self.idf is not None and self.vocab
        rows = list(token_lists)
        mat = np.zeros((len(rows), len(self.vocab)), dtype=np.float32)
        for r, toks in enumerate(rows):
            if not toks:
                continue
            counts: dict[int, int] = {}
            for t in toks:
                idx = self.vocab.get(t)
                if idx is not None:
                    counts[idx] = counts.get(idx, 0) + 1
            for idx, c in counts.items():
                # 次线性 TF：抑制长文档中高频词的支配
                mat[r, idx] = (1.0 + math.log(c)) * self.idf[idx]
        return _l2_normalize(mat)

    @staticmethod
    def _randomized_svd(x: np.ndarray, k: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
        """Halko-Martinsson-Tropp 随机化 SVD，返回 (奇异值, Vᵀ 前 k 行)。"""
        n_docs, n_terms = x.shape
        k = max(1, min(k, min(n_docs, n_terms) - 1 if min(n_docs, n_terms) > 1 else 1))
        oversample = min(20, max(2, n_terms - k))
        rng = np.random.default_rng(seed)
        omega = rng.standard_normal((n_terms, k + oversample)).astype(np.float32)
        y = x @ omega
        q, _ = np.linalg.qr(y)
        b = q.T @ x
        _, s, vt = np.linalg.svd(b, full_matrices=False)
        return s[:k].astype(np.float32), vt[:k].astype(np.float32)

    def fit(self, texts: Sequence[str]) -> "LSAEmbedder":
        token_lists = [tokenize(t) for t in texts]
        self._build_vocab(token_lists)
        if not self.vocab:
            raise RuntimeError("LSA 词表为空：语料过短或 min_df 过高")
        x = self._tfidf_matrix(token_lists)
        self.n_docs_fit = x.shape[0]
        target_k = min(self.dim, min(x.shape) - 1) if min(x.shape) > 1 else 1
        self.singular, self.components = self._randomized_svd(x, max(1, target_k), self.seed)
        self.dim = int(self.components.shape[0])
        self.fitted = True
        self.save()
        log.info("LSA fitted: docs=%d terms=%d dim=%d top_sv=%.3f",
                 self.n_docs_fit, len(self.vocab), self.dim, float(self.singular[0]))
        return self

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not self.fitted:
            self.load()
        if not self.fitted:
            raise RuntimeError("LSA 模型尚未拟合，请先执行导入（ingest）")
        x = self._tfidf_matrix([tokenize(t) for t in texts])
        return _l2_normalize(x @ self.components.T)

    # -- 持久化 -------------------------------------------------------------
    def save(self) -> None:
        payload = {
            "vocab": self.vocab,
            "idf": self.idf,
            "components": self.components,
            "singular": self.singular,
            "dim": self.dim,
            "n_docs_fit": self.n_docs_fit,
            "min_df": self.min_df,
            "max_features": self.max_features,
            "seed": self.seed,
            "has_jieba": _HAS_JIEBA,
        }
        np.savez_compressed(str(self.model_path) + ".npz", **{
            k: (json.dumps(v, ensure_ascii=False) if k == "vocab" else v)
            for k, v in payload.items()
        })
        log.info("LSA model saved to %s.npz", self.model_path)

    def load(self) -> bool:
        path = str(self.model_path) + ".npz"
        try:
            with np.load(path, allow_pickle=False) as z:
                self.vocab = json.loads(str(z["vocab"]))
                self.idf = z["idf"]
                self.components = z["components"]
                self.singular = z["singular"] if "singular" in z else None
                self.dim = int(z["dim"])
                self.n_docs_fit = int(z["n_docs_fit"])
                self.fitted = True
            log.info("LSA model loaded: dim=%d terms=%d", self.dim, len(self.vocab))
            return True
        except FileNotFoundError:
            # 首次运行还没拟合过模型，属于正常情况，不该用 warning 吓人
            log.info("还没有已拟合的 LSA 模型（%s），将在首次导入时拟合", path)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("LSA 模型无法加载（%s）：%s", path, exc)
            return False

    def describe(self) -> dict:
        base = super().describe()
        base.update({
            "vocab_size": len(self.vocab),
            "n_docs_fit": self.n_docs_fit,
            "top_singular_value": float(self.singular[0]) if self.singular is not None else None,
            "jieba": _HAS_JIEBA,
            "method": "tfidf + randomized truncated SVD (LSA)",
        })
        return base


def _http_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 {url}: {exc.reason}") from exc


class OllamaEmbedder(BaseEmbedder):
    """调用本机 Ollama 的 /api/embed（批式），旧版本自动回退到 /api/embeddings。"""

    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or config.OLLAMA_EMBED_MODEL
        self.dim = 0

    def _batch(self, texts: Sequence[str]) -> np.ndarray:
        try:
            out = _http_json(f"{self.base_url}/api/embed",
                             {"model": self.model, "input": list(texts)})
            embs = out.get("embeddings")
            if embs:
                return np.asarray(embs, dtype=np.float32)
        except RuntimeError as exc:
            log.warning("/api/embed 失败，回退 /api/embeddings：%s", exc)
        rows = []
        for t in texts:
            out = _http_json(f"{self.base_url}/api/embeddings",
                             {"model": self.model, "prompt": t})
            rows.append(out["embedding"])
        return np.asarray(rows, dtype=np.float32)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        chunks = []
        bs = max(1, config.EMBED_BATCH_SIZE)
        for i in range(0, len(texts), bs):
            chunks.append(self._batch(texts[i : i + bs]))
        mat = np.vstack(chunks) if chunks else np.zeros((0, 0), dtype=np.float32)
        if not self.dim and mat.size:
            self.dim = int(mat.shape[1])
        return _l2_normalize(mat)

    def probe_dim(self) -> int:
        if not self.dim:
            self.encode(["维度探测"])
        return self.dim


class SentenceTransformerEmbedder(BaseEmbedder):
    name = "sentence_transformers"

    def __init__(self, model_path: str | None = None, device: str | None = None) -> None:
        self.model_path = model_path or config.ST_MODEL_PATH
        self.device = device or config.ST_DEVICE
        self._model = None
        self.dim = 0

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # 延迟导入

            self._model = SentenceTransformer(self.model_path, device=self.device)
            self.dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        model = self._load()
        mat = model.encode(list(texts), normalize_embeddings=True,
                           batch_size=max(1, config.EMBED_BATCH_SIZE),
                           show_progress_bar=False)
        return np.asarray(mat, dtype=np.float32)

    def probe_dim(self) -> int:
        self._load()
        return self.dim


class OpenAICompatEmbedder(BaseEmbedder):
    name = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None) -> None:
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key or config.OPENAI_API_KEY
        self.model = model or config.OPENAI_EMBED_MODEL
        self.dim = 0

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not self.base_url:
            raise RuntimeError("OPENAI_BASE_URL 未配置")
        out = _http_json(
            f"{self.base_url}/embeddings",
            {"model": self.model, "input": list(texts)},
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else None,
        )
        rows = [item["embedding"] for item in sorted(out["data"], key=lambda d: d.get("index", 0))]
        mat = np.asarray(rows, dtype=np.float32)
        if not self.dim and mat.size:
            self.dim = int(mat.shape[1])
        return _l2_normalize(mat)

    def probe_dim(self) -> int:
        if not self.dim:
            self.encode(["维度探测"])
        return self.dim


_EMBEDDER: BaseEmbedder | None = None
_EMBEDDER_LOCK = threading.Lock()


def get_embedder(force_reload: bool = False) -> BaseEmbedder:
    """按配置返回单例 embedder。

    必须是**真**单例：LSA 模型是 6417×256 的 float32（约 6.5 MB npz），
    每次检索都重新 load 一遍会给每个查询白白加上约 45 ms 磁盘 IO。

    这里原来写错过一次，而且错得很隐蔽：函数声明的是 `global _EMBEDER`
    （少一个 D），末尾 `_EMBEDER = emb` 写进的是另一个全局名字，
    被检查的 `_EMBEDDER` 永远是 None —— 缓存形同不存在，但没有任何报错。
    scripts/profile_query.py 就是用来抓这类「不报错的性能洞」的。

    Web 层是多线程的（ThreadingHTTPServer），所以用锁做双检，
    避免并发首查时多个线程同时去读模型文件。
    """
    global _EMBEDDER
    if _EMBEDDER is not None and not force_reload:
        return _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is not None and not force_reload:
            return _EMBEDDER
        backend = config.EMBED_BACKEND
        emb: BaseEmbedder
        if backend == "lsa":
            lsa = LSAEmbedder()
            lsa.load()
            emb = lsa
        elif backend == "ollama":
            emb = OllamaEmbedder()
        elif backend in ("sentence_transformers", "sentence-transformers", "st"):
            emb = SentenceTransformerEmbedder()
        elif backend == "openai":
            emb = OpenAICompatEmbedder()
        else:
            raise config.ConfigError(f"未知 EMBED_BACKEND: {backend}")
        _EMBEDDER = emb
        log.info("embedder ready: %s", emb.describe())
        return emb


def resolve_dim() -> int:
    """建表前确定向量维度。

    LSA 用配置值；神经后端必须实际探测一次，否则 seekdb 的 VECTOR(n) 维度不匹配会写入失败。
    """
    emb = get_embedder()
    if isinstance(emb, LSAEmbedder):
        return emb.dim or config.EMBED_DIM
    prober = getattr(emb, "probe_dim", None)
    if prober:
        return int(prober())
    return int(emb.dim or config.EMBED_DIM)
