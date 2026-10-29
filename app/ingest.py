"""导入管线：文件 → 结构化分块 → 向量 → seekdb。

两种模式：
  rebuild     （默认）清空后全量重建。LSA 后端需要在整个语料上拟合，
              全量重建保证所有向量处于同一个语义空间，可比性最好。
  incremental 追加导入。沿用已拟合的向量模型，只编码新文档；
              适合日常增量入库，代价是语义空间会随语料增长产生轻微漂移。
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import chunking, config, db, embedder, schema

log = logging.getLogger("seeklit.ingest")

SUPPORTED_EXT = {".md", ".markdown", ".txt", ".log", ".csv", ".tsv", ".pdf"}


@dataclass
class FileEntry:
    path: Path
    rel: str
    sha256: str
    size: int
    doc_kind: str
    tags: str


@dataclass
class ImportReport:
    mode: str = "rebuild"
    files_total: int = 0
    files_imported: int = 0
    files_skipped: int = 0
    chunks: int = 0
    chars: int = 0
    embed_seconds: float = 0.0
    db_seconds: float = 0.0
    parse_seconds: float = 0.0
    total_seconds: float = 0.0
    embedder: dict = field(default_factory=dict)
    dim: int = 0
    per_file: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "files_total": self.files_total,
            "files_imported": self.files_imported,
            "files_skipped": self.files_skipped,
            "chunks": self.chunks,
            "chars": self.chars,
            "timings": {
                "parse_s": round(self.parse_seconds, 3),
                "embed_s": round(self.embed_seconds, 3),
                "db_s": round(self.db_seconds, 3),
                "total_s": round(self.total_seconds, 3),
            },
            "embedder": self.embedder,
            "dim": self.dim,
            "per_file": self.per_file,
            "errors": self.errors,
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan_corpus(root: Path | None = None) -> list[FileEntry]:
    root = Path(root or config.CORPUS_DIR)
    if not root.exists():
        raise FileNotFoundError(f"语料目录不存在：{root}")
    entries: list[FileEntry] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXT:
            continue
        if path.name == "corpus_raw.tar.gz" or ".git" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        folder = path.parent.name.lower()
        kind = chunking.KIND_BY_DIR.get(folder) or chunking.KIND_BY_EXT.get(path.suffix.lower(), "note")
        tags = ",".join(sorted({folder, kind}))
        entries.append(FileEntry(path=path, rel=rel, sha256=_sha256(path),
                                 size=path.stat().st_size, doc_kind=kind, tags=tags))
    return entries


def _next_chunk_id() -> int:
    if not schema.has_table("chunk"):
        return 1
    cur = db.scalar("SELECT COALESCE(MAX(chunk_id), 0) AS m FROM `chunk`", default=0)
    return int(cur or 0) + 1


def existing_hashes() -> set[str]:
    if not schema.has_table("doc"):
        return set()
    rows = db.query("SELECT sha256 FROM `doc`")
    return {r["sha256"] for r in rows if r.get("sha256")}


def run_import(root: Path | None = None, mode: str = "rebuild",
               limit: int | None = None, progress=None) -> ImportReport:
    """执行导入。progress(step:str, done:int, total:int) 可选，用于前端进度条。"""
    t_start = time.perf_counter()
    report = ImportReport(mode=mode)

    entries = scan_corpus(root)
    if limit:
        entries = entries[:limit]
    report.files_total = len(entries)

    if mode == "rebuild":
        # 首次运行时表还不存在，DELETE 会报 1146；ensure_schema 稍后会建好
        if schema.has_table("chunk"):
            db.execute("DELETE FROM `chunk`")
        if schema.has_table("doc"):
            db.execute("DELETE FROM `doc`")
        known: set[str] = set()
    else:
        known = existing_hashes()

    def _progress(step: str, done: int, total: int) -> None:
        if progress:
            try:
                progress(step, done, total)
            except Exception:  # noqa: BLE001
                pass

    # ---- 1) 解析 + 分块 ---------------------------------------------------
    parsed: list[tuple[FileEntry, list[chunking.Chunk], str]] = []
    parse_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    for i, entry in enumerate(entries, start=1):
        if entry.sha256 in known:
            report.files_skipped += 1
            continue
        tf = time.perf_counter()
        try:
            chunks, title, kind = chunking.chunk_file(entry.path)
        except Exception as exc:  # noqa: BLE001
            log.exception("解析失败 %s", entry.rel)
            report.errors.append({"file": entry.rel, "stage": "parse", "error": str(exc)[:300]})
            continue
        parse_timings[entry.rel] = time.perf_counter() - tf
        if not chunks:
            report.errors.append({"file": entry.rel, "stage": "parse", "error": "未产生任何分块"})
            continue
        if kind and kind != entry.doc_kind:
            entry.doc_kind = kind
        parsed.append((entry, chunks, title))
        _progress("parse", i, len(entries))
    report.parse_seconds = time.perf_counter() - t0

    all_texts = [c.text for _e, chunks, _t in parsed for c in chunks]
    if not all_texts:
        report.total_seconds = time.perf_counter() - t_start
        return report

    # ---- 2) 向量化 --------------------------------------------------------
    emb = embedder.get_embedder()
    dim = schema_dim(emb, all_texts)
    report.dim = dim
    schema.ensure_schema(dim)

    t0 = time.perf_counter()
    if isinstance(emb, embedder.LSAEmbedder):
        if mode == "rebuild" or not emb.fitted:
            emb.fit(all_texts)
        vectors = np.vstack([
            emb.encode(all_texts[i : i + config.EMBED_BATCH_SIZE])
            for i in range(0, len(all_texts), config.EMBED_BATCH_SIZE)
        ])
    else:
        vectors = np.vstack([
            emb.encode(all_texts[i : i + config.EMBED_BATCH_SIZE])
            for i in range(0, len(all_texts), config.EMBED_BATCH_SIZE)
        ])
    report.embed_seconds = time.perf_counter() - t0
    report.embedder = emb.describe()
    _progress("embed", 1, 1)

    if vectors.shape[1] != dim:
        raise RuntimeError(f"向量维度不一致：表为 VECTOR({dim})，实际得到 {vectors.shape[1]}")

    # ---- 3) 写入 seekdb ---------------------------------------------------
    t0 = time.perf_counter()
    chunk_id = _next_chunk_id() if mode == "incremental" else 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc_rows: list[tuple] = []
    chunk_rows: list[tuple] = []
    offset = 0
    for entry, chunks, title in parsed:
        doc_id = entry.sha256[:16]
        doc_chars = sum(c.n_chars for c in chunks)
        lang = chunking.detect_lang(" ".join(c.text[:200] for c in chunks[:3]))
        doc_rows.append((
            doc_id, title[:500], entry.rel, entry.doc_kind, lang,
            len(chunks), doc_chars, entry.sha256, entry.tags[:500], now,
        ))
        for c in chunks:
            vec = db.vector_literal(vectors[offset])
            chunk_rows.append((
                chunk_id, doc_id, title[:500], entry.doc_kind, entry.rel, lang,
                c.page_no, (c.section_path or "")[:1000], c.line_start, c.line_end,
                c.chunk_index, entry.tags[:500], c.text,
                hashlib.md5(c.text.encode("utf-8")).hexdigest(), vec, now,
            ))
            chunk_id += 1
            offset += 1
        report.per_file.append({
            "file": entry.rel, "title": title[:160], "kind": entry.doc_kind,
            "chunks": len(chunks), "chars": doc_chars, "lang": lang,
            "parse_seconds": round(parse_timings.get(entry.rel, 0.0), 3),
        })
        report.files_imported += 1
        report.chunks += len(chunks)
        report.chars += doc_chars

    db.execute_many(
        "INSERT INTO `doc` (doc_id,title,source_path,doc_kind,lang,n_chunks,n_chars,"
        "sha256,tags,imported_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        doc_rows,
    )
    db.execute_many(
        "INSERT INTO `chunk` (chunk_id,doc_id,doc_title,doc_kind,source_path,lang,"
        "page_no,section_path,line_start,line_end,chunk_index,tags,content,content_hash,"
        "emb,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        chunk_rows,
    )
    report.db_seconds = time.perf_counter() - t0
    report.total_seconds = time.perf_counter() - t_start
    _progress("done", 1, 1)

    log.info("import complete: files=%d chunks=%d dim=%d embed=%.1fs db=%.1fs",
             report.files_imported, report.chunks, dim,
             report.embed_seconds, report.db_seconds)
    return report


def schema_dim(emb, sample_texts: list[str]) -> int:
    """确定建表维度。

    LSA：维度由拟合结果决定（受语料规模上限约束），因此先在一个小样本上试拟合；
    神经后端：实际探测一次，避免 VECTOR(n) 与模型输出维度不匹配。
    """
    if isinstance(emb, embedder.LSAEmbedder):
        if emb.fitted and emb.dim:
            return emb.dim
        probe = embedder.LSAEmbedder(dim=config.EMBED_DIM)
        try:
            probe.fit(sample_texts[: min(len(sample_texts), 400)])
            return probe.dim
        except Exception as exc:  # noqa: BLE001
            log.warning("LSA 维度探测失败，回退配置值 %d：%s", config.EMBED_DIM, exc)
            return config.EMBED_DIM
    prober = getattr(emb, "probe_dim", None)
    if prober:
        return int(prober())
    return int(emb.dim or config.EMBED_DIM)


def corpus_stats() -> dict:
    """语料库概览：文档数、分块数、按类型/语言分布。"""
    if not schema.has_table("doc"):
        return {"docs": 0, "chunks": 0, "chars": 0, "by_kind": [], "by_lang": []}
    docs = db.scalar("SELECT COUNT(*) AS c FROM `doc`", default=0)
    chunks = db.scalar("SELECT COUNT(*) AS c FROM `chunk`", default=0)
    chars = db.scalar("SELECT COALESCE(SUM(n_chars),0) AS c FROM `doc`", default=0)
    by_kind = db.query(
        "SELECT doc_kind, COUNT(*) AS docs, SUM(n_chunks) AS chunks "
        "FROM `doc` GROUP BY doc_kind ORDER BY docs DESC"
    )
    by_lang = db.query("SELECT lang, COUNT(*) AS chunks FROM `chunk` GROUP BY lang")
    return {
        "docs": int(docs or 0),
        "chunks": int(chunks or 0),
        "chars": int(chars or 0),
        "by_kind": by_kind,
        "by_lang": by_lang,
    }


def list_docs() -> list[dict]:
    return db.query(
        "SELECT doc_id,title,source_path,doc_kind,lang,n_chunks,n_chars,tags,imported_at "
        "FROM `doc` ORDER BY doc_kind, title"
    )


def delete_doc(doc_id: str) -> int:
    n = db.execute("DELETE FROM `chunk` WHERE doc_id=%s", (doc_id,))
    db.execute("DELETE FROM `doc` WHERE doc_id=%s", (doc_id,))
    return n
