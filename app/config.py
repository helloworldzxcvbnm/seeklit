"""溯文 SeekLit —— 运行配置。

全部配置项都可以通过环境变量或项目根目录的 .env 文件覆盖，
不依赖任何外部配置中心，方便评审一键复现。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DATA_DIR = Path(os.getenv("SEEKLIT_DATA_DIR", PROJECT_ROOT / "data"))
CORPUS_DIR = Path(os.getenv("SEEKLIT_CORPUS_DIR", PROJECT_ROOT / "corpus_raw"))
LOG_DIR = Path(os.getenv("SEEKLIT_LOG_DIR", PROJECT_ROOT / "logs"))

for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _load_dotenv() -> None:
    """极简 .env 解析：不引入 python-dotenv 依赖，避免评审环境缺包。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# seekdb 连接（MySQL 协议）
# ---------------------------------------------------------------------------
SEEKDB_HOST = os.getenv("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = _int("SEEKDB_PORT", 10100)
SEEKDB_USER = os.getenv("SEEKDB_USER", "root@sys")
SEEKDB_PASSWORD = os.getenv("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.getenv("SEEKDB_DATABASE", "seeklit")
# seekdb 单条 SQL 的超时（微秒）。混合检索带窗口函数，给足余量。
SEEKDB_QUERY_TIMEOUT = _int("SEEKDB_QUERY_TIMEOUT", 100_000_000)

# ---------------------------------------------------------------------------
# 向量化后端
#   lsa                  —— 默认。离线 TF-IDF + 截断 SVD，零外部依赖、零下载
#   ollama               —— 调用本机 Ollama 的 /api/embed（如 bge-m3）
#   sentence_transformers—— 调用本地或 HF 的 sentence-transformers 模型
#   openai               —— 任意 OpenAI 兼容 /v1/embeddings 接口
# ---------------------------------------------------------------------------
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "lsa").strip().lower()
EMBED_DIM = _int("EMBED_DIM", 256)

# LSA 参数
LSA_MIN_DF = _int("LSA_MIN_DF", 2)
LSA_MAX_FEATURES = _int("LSA_MAX_FEATURES", 20000)
LSA_MODEL_PATH = Path(os.getenv("LSA_MODEL_PATH", DATA_DIR / "lsa_model.pt"))

# Ollama 参数
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")

# sentence-transformers 参数
ST_MODEL_PATH = os.getenv("ST_MODEL_PATH", "BAAI/bge-small-zh-v1.5")
ST_DEVICE = os.getenv("ST_DEVICE", "cpu")

# OpenAI 兼容参数
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# ---------------------------------------------------------------------------
# 文档解析 / 分块
# ---------------------------------------------------------------------------
CHUNK_TARGET_CHARS = _int("CHUNK_TARGET_CHARS", 520)
CHUNK_MAX_CHARS = _int("CHUNK_MAX_CHARS", 900)
CHUNK_OVERLAP_CHARS = _int("CHUNK_OVERLAP_CHARS", 80)
PDF_MAX_PAGES = _int("PDF_MAX_PAGES", 0)  # 0 = 不限
EMBED_BATCH_SIZE = _int("EMBED_BATCH_SIZE", 32)
DB_INSERT_BATCH = _int("DB_INSERT_BATCH", 200)

# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
SEARCH_TOP_K = _int("SEARCH_TOP_K", 8)
SEARCH_CANDIDATE_K = _int("SEARCH_CANDIDATE_K", 60)
RRF_K = _int("RRF_K", 60)
DEFAULT_SEMANTIC_WEIGHT = _float("DEFAULT_SEMANTIC_WEIGHT", 0.6)
# seekdb 1.4.0.0 的 dbms_hybrid_search 在 knn 里只接受 k，
# 传 num_candidates 会生成非法 SQL（已在 docs/SEEKDB_EVIDENCE.md 记录）。
NATIVE_HYBRID_ENABLED = _bool("NATIVE_HYBRID_ENABLED", True)

# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = _int("API_PORT", 8090)
# 站点体验入口用的只读令牌；留空表示不校验
API_TOKEN = os.getenv("SEEKLIT_API_TOKEN", "")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

# 前端快照导出目录（Qoder Sites 站点内嵌的离线演示数据）
SNAPSHOT_DIR = Path(os.getenv("SEEKLIT_SNAPSHOT_DIR", PROJECT_ROOT / "snapshot"))

# 出错时是否打印完整 traceback
DEBUG = _bool("SEEKLIT_DEBUG", False)


class ConfigError(RuntimeError):
    pass


def describe() -> dict:
    """返回当前生效的配置摘要，用于 /api/health 与前端「运行环境」面板。"""
    return {
        "embed_backend": EMBED_BACKEND,
        "embed_dim": EMBED_DIM,
        "seekdb": {
            "host": SEEKDB_HOST,
            "port": SEEKDB_PORT,
            "database": SEEKDB_DATABASE,
            "user": SEEKDB_USER,
        },
        "chunking": {
            "target_chars": CHUNK_TARGET_CHARS,
            "max_chars": CHUNK_MAX_CHARS,
            "overlap_chars": CHUNK_OVERLAP_CHARS,
        },
        "search": {
            "top_k": SEARCH_TOP_K,
            "candidate_k": SEARCH_CANDIDATE_K,
            "rrf_k": RRF_K,
            "default_semantic_weight": DEFAULT_SEMANTIC_WEIGHT,
            "native_hybrid_enabled": NATIVE_HYBRID_ENABLED,
        },
        "corpus_dir": str(CORPUS_DIR),
        "data_dir": str(DATA_DIR),
        "api": {
            "host": API_HOST,
            "port": API_PORT,
            # Web 层零外部依赖：直接用标准库 http.server，
            # 部署机出网带宽只有十几 KB/s，装不动 FastAPI 的依赖树。
            "runtime": "python http.server (stdlib)",
            "token_auth": bool(API_TOKEN.strip()),
            "snapshot_dir": str(SNAPSHOT_DIR),
        },
    }
