"""检索结果快照导出。

用途：Qoder Sites 上的公开站点不直连服务器，而是内嵌一份**离线快照**。
快照里的每个分数、每段 SQL、每条排名都来自服务器上 seekdb 的真实查询结果，
不是模拟数据。站点拿到快照后可以在浏览器里：

  · 切换 semantic / keyword / hybrid 三种模式
  · 拖动语义权重滑块，用快照中记录的真实 vsim 与 bm25 重新做加权融合
  · 调整 RRF 的 rank_constant，用真实排名重新计算 RRF 分数
  · 展开查看 seekdb 实际执行/生成的 SQL

为了让上述交互都基于真实数据，导出时除了三路结果，还会额外补齐
「候选池」中每个分块的原始 vsim 与 bm25（缺失的分数会单独回查一次 seekdb）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal
from typing import Any, Sequence

from . import config, db, embedder, ingest, schema, search

log = logging.getLogger("seeklit.snapshot")

# 预置检索样例：每条都对应语料里真实存在的内容，并标注它想说明什么
PRESET_QUERIES: list[dict[str, str]] = [
    {
        "text": "持续小幅慢漂移用哪种方法在线检测",
        "intent": "语义为主：正文里的术语是 CUSUM / EWMA，并不包含「在线检测」这个词组",
    },
    {
        "text": "CUSUM 的 k 和 h 怎么标定",
        "intent": "关键词为主：k、h、标定、ARL 都是精确词元，向量检索容易漏",
    },
    {
        "text": "单点毛刺应该用哪个方法",
        "intent": "语义 + 关键词：毛刺对应 Hampel / 滑动 MAD，需要跨措辞匹配",
    },
    {
        "text": "报警拖尾导致误判故障仍在持续",
        "intent": "语义为主：描述的是 EWMA 故障结束后拖尾的缺点",
    },
    {
        "text": "AGC 上升 RSL 下降属于哪一类异常",
        "intent": "关系违背型异常，缩写词必须靠全文索引精确命中",
    },
    {
        "text": "哪些方法对持续电平型故障结构性失明",
        "intent": "跨段落归纳：答案分散在多个方法条目里",
    },
    {
        "text": "误报预算怎么用蒙特卡洛重新标定",
        "intent": "关键词 + 语义：自相关下 iid 临界值系统性低估误报",
    },
    {
        "text": "unsupervised anomaly detection for spacecraft telemetry",
        "intent": "英文论文召回：验证中英文混排语料上的检索能力",
    },
]


def list_preset_queries() -> list[dict[str, str]]:
    return [dict(q) for q in PRESET_QUERIES]


def _bm25_for(chunk_ids: Sequence[int], qtext: str) -> dict[int, float]:
    """回查指定分块的 BM25 分数（用于补全候选池中未被全文分支召回的条目）。"""
    ids = [int(c) for c in chunk_ids]
    if not ids:
        return {}
    marks = ",".join(["%s"] * len(ids))
    expr = search._match_expr()
    sql = (f"SELECT chunk_id, {expr} AS bm25 FROM `chunk` "
           f"WHERE chunk_id IN ({marks})")
    rows = db.query(sql, [qtext, *ids])
    return {int(r["chunk_id"]): float(r.get("bm25") or 0.0) for r in rows}


def _cosine_for(chunk_ids: Sequence[int], qvec: Sequence[float]) -> dict[int, float]:
    """回查指定分块与查询向量的余弦相似度。"""
    ids = [int(c) for c in chunk_ids]
    if not ids:
        return {}
    marks = ",".join(["%s"] * len(ids))
    literal = db.vector_literal(qvec)
    sql = (f"SELECT chunk_id, cosine_distance(`emb`, '{literal}') AS d FROM `chunk` "
           f"WHERE chunk_id IN ({marks})")
    rows = db.query(sql, ids)
    return {int(r["chunk_id"]): round(max(0.0, 1.0 - float(r["d"] or 1.0)), 6) for r in rows}


def _json_default(o: Any) -> Any:
    """JSON 兜底转换。

    seekdb 的 DECIMAL 列（search_log.weight、corpus_stats 里的 SUM(...)）
    经 pymysql 回来是 decimal.Decimal，标准 json 不认识，直接抛 TypeError。
    统一在这里收敛，避免每处调用点都记得写 default=str —— 而 default=str
    会把数字变成字符串，前端还得再转一次。
    """
    if isinstance(o, Decimal):
        f = float(o)
        return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


def to_json(obj: Any, *, compact: bool = True) -> str:
    separators = (",", ":") if compact else (", ", ": ")
    indent = None if compact else 2
    return json.dumps(obj, ensure_ascii=False, default=_json_default,
                      separators=separators, indent=indent)


def _slim(hit: dict, content_chars: int = 1400) -> dict:
    keep = ("chunk_id", "doc_id", "doc_title", "doc_kind", "source_path", "lang",
            "page_no", "section_path", "line_start", "line_end", "tags", "content",
            "vec_score", "bm25", "fused", "vec_rank", "bm25_rank", "final_rank",
            "matched_terms")
    out = {k: hit.get(k) for k in keep if hit.get(k) is not None}
    # 公开站点只需要展示片段；截断正文能把快照从十几 MB 压到 1 MB 量级。
    # 截断处显式标注，避免看起来像「seekdb 只返回了这么点内容」。
    c = out.get("content")
    if isinstance(c, str) and len(c) > content_chars:
        out["content"] = c[:content_chars]
        out["content_truncated_from"] = len(c)
    return out


# SQL 里的向量字面量：'[0.123, -0.456, …]'（256 维，约 2.5 KB）
_VECTOR_LITERAL = re.compile(r"'\[-?\d[\d\s,\.eE+-]*\]'")


def _compact_sql(sqls: Sequence[str], dim: int) -> list[str]:
    """把 SQL 里的 256 维向量字面量换成占位符。

    快照要挂到公开静态站点上，一条 SQL 里塞两个完整向量字面量就是 5 KB，
    8 个问题 × 5 个权重变体会把文件撑到十几 MB，评审根本打不开。

    这里不删信息、只做**去重**：完整向量在同一个查询快照里存一份
    （`query_vector` 字段），SQL 中用占位符指过去，任何人拿这份快照都能
    逐字节还原出 seekdb 当时真正执行的语句。
    """
    out = []
    for s in sqls or []:
        if not s:
            out.append(s)
            continue
        out.append(_VECTOR_LITERAL.sub(f"'[<{dim} 维查询向量，见本查询的 query_vector 字段>]'", s))
    return out


def _slim_native_params(params: dict | None, dim: int) -> dict | None:
    """native DSL 里的 query_vector 同样只留一份引用，不重复塞 256 个浮点数。"""
    if not params:
        return None
    p = dict(params)
    knn = dict(p.get("knn") or {})
    if "query_vector" in knn:
        knn["query_vector"] = f"<{dim} 维向量，见本查询的 query_vector 字段>"
        p["knn"] = knn
    return p


def build_snapshot(queries: Sequence[str] | None = None, top_k: int = 10,
                   candidate_k: int = 40, weights: Sequence[float] = (0.0, 0.3, 0.5, 0.7, 1.0)) -> dict:
    """构建完整快照。所有分数均来自真实 seekdb 查询。"""
    emb = embedder.get_embedder()
    presets = list_preset_queries()
    if queries:
        wanted = list(queries)
        items = [next((p for p in presets if p["text"] == q), {"text": q, "intent": ""}) for q in wanted]
    else:
        items = presets

    query_snaps: list[dict] = []
    dim = int(emb.dim or config.EMBED_DIM)
    for item in items:
        qtext = item["text"]
        t0 = time.perf_counter()
        qvec = emb.encode_one(qtext)

        sem = search.search(qtext, mode="semantic", top_k=top_k,
                            candidate_k=candidate_k, log_query=False)
        kw = search.search(qtext, mode="keyword", top_k=top_k,
                           candidate_k=candidate_k, log_query=False)
        rrf = search.search(qtext, mode="hybrid", fusion="rrf", top_k=top_k,
                            candidate_k=candidate_k, log_query=False)

        # 候选池：两路候选的并集，补齐彼此的原始分数
        pool_ids: list[int] = []
        for res in (sem, kw):
            for h in res["hits"]:
                cid = int(h["chunk_id"])
                if cid not in pool_ids:
                    pool_ids.append(cid)
        sem_big = search.search(qtext, mode="semantic", top_k=candidate_k,
                                candidate_k=candidate_k, log_query=False)
        kw_big = search.search(qtext, mode="keyword", top_k=candidate_k,
                               candidate_k=candidate_k, log_query=False)
        pool: dict[int, dict] = {}
        for res in (sem_big, kw_big):
            for h in res["hits"]:
                cid = int(h["chunk_id"])
                entry = pool.setdefault(cid, _slim(h))
                if h.get("vec_score") is not None:
                    entry["vsim"] = h["vec_score"]
                if h.get("bm25") is not None:
                    entry["bm25_raw"] = h["bm25"]
        missing_bm25 = [cid for cid, e in pool.items() if "bm25_raw" not in e]
        if missing_bm25:
            for cid, score in _bm25_for(missing_bm25, qtext).items():
                pool.setdefault(cid, {})["bm25_raw"] = score
        missing_v = [cid for cid, e in pool.items() if "vsim" not in e]
        if missing_v:
            for cid, score in _cosine_for(missing_v, qvec).items():
                pool.setdefault(cid, {})["vsim"] = score

        pool_list = []
        for cid, e in pool.items():
            e = dict(e)
            e["chunk_id"] = cid
            e.setdefault("vsim", 0.0)
            e.setdefault("bm25_raw", 0.0)
            pool_list.append(e)
        pool_list.sort(key=lambda x: -(x["vsim"] + x["bm25_raw"]))

        weighted_variants = []
        for w in weights:
            res = search.search(qtext, mode="hybrid", fusion="weighted",
                                semantic_weight=float(w), top_k=top_k,
                                candidate_k=candidate_k, log_query=False)
            weighted_variants.append({
                "semantic_weight": float(w),
                "execution": res["execution"],
                "elapsed_ms": res["elapsed_ms"],
                "sql": _compact_sql(res["sql"], dim),
                "ids": [int(h["chunk_id"]) for h in res["hits"]],
            })

        compare = search.compare_modes(qtext, top_k=top_k)

        query_snaps.append({
            "text": qtext,
            "intent": item.get("intent", ""),
            "build_seconds": round(time.perf_counter() - t0, 3),
            # 完整查询向量只在这里存一份，SQL 里的字面量用占位符指过来
            "query_vector": [round(float(v), 8) for v in qvec],
            "pool": pool_list,
            "results": {
                "semantic": {
                    "ids": [int(h["chunk_id"]) for h in sem["hits"]],
                    "hits": [_slim(h) for h in sem["hits"]],
                    "sql": _compact_sql(sem["sql"], dim), "elapsed_ms": sem["elapsed_ms"],
                    "execution": sem["execution"],
                },
                "keyword": {
                    "ids": [int(h["chunk_id"]) for h in kw["hits"]],
                    "hits": [_slim(h) for h in kw["hits"]],
                    "sql": _compact_sql(kw["sql"], dim), "elapsed_ms": kw["elapsed_ms"],
                    "execution": kw["execution"],
                },
                "hybrid_rrf": {
                    "ids": [int(h["chunk_id"]) for h in rrf["hits"]],
                    "hits": [_slim(h) for h in rrf["hits"]],
                    "sql": _compact_sql(rrf["sql"], dim), "elapsed_ms": rrf["elapsed_ms"],
                    "execution": rrf["execution"],
                    "native_params": _slim_native_params(rrf.get("native_params"), dim),
                },
                "hybrid_weighted": weighted_variants,
            },
            "compare": {
                "agreement": compare["agreement"],
                "rows": compare["rows"],
            },
        })
        log.info("snapshot query done: %s (%.1fs, pool=%d)",
                 qtext, time.perf_counter() - t0, len(pool_list))

    ddl = (schema.table_info() or [{}])[0].get("ddl")
    payload = {
        "schema_version": 1,
        "app": "溯文 SeekLit",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "本快照中的全部分数、排名与 SQL 均由服务器上真实运行的 seekdb 查询产生；"
                "站点内的权重滑块与 rank_constant 调节基于快照记录的原始分数在浏览器中重新计算。",
        "sql_note": "SQL 里的 256 维向量字面量被替换成占位符以压缩体积，"
                    "完整向量在每个查询的 query_vector 字段里，据此可逐字节还原原始语句。",
        "seekdb_version": db.server_version(),
        "embedder": emb.describe(),
        "config": config.describe(),
        "chunk_table_ddl": ddl,
        "corpus": ingest.corpus_stats(),
        "documents": ingest.list_docs(),
        "queries": query_snaps,
    }
    payload["_size_bytes"] = len(to_json(payload))
    return payload
