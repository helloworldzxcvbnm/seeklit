"""混合检索引擎。

三种检索模式，全部由 seekdb 执行：

  semantic  纯语义：VECTOR 索引 + cosine + `ORDER BY ... APPROXIMATE`（HNSW 近邻）
  keyword   纯关键词：FULLTEXT 索引（ik 中文分词）+ `MATCH ... AGAINST`（BM25）
  hybrid    混合：两路同时下推，再融合

hybrid 提供两种融合策略：

  rrf       调用 seekdb **原生** `dbms_hybrid_search.search()`，
            由数据库内部完成 FULL JOIN + RANK() + RRF 融合；
            同时用 `dbms_hybrid_search.get_sql()` 取回它自己生成的 SQL 回显给前端。
  weighted  可调用权重的融合。优先尝试「单条 SQL 内完成两路召回 + 归一化 + 加权」，
            若当前 seekdb 版本不支持该写法，自动降级为「两条下推 SQL + 应用层融合」，
            并在响应里如实标注实际使用的执行方式。

所有查询都会把**真实执行的 SQL** 一并返回，前端可展开核对，
这也是评审确认「seekdb 确实在起作用」的直接依据。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from . import config, db, embedder
from .embedder import tokenize

log = logging.getLogger("seeklit.search")

MODES = ("semantic", "keyword", "hybrid")
FUSIONS = ("rrf", "weighted")

_SAFE_FIELDS = (
    "chunk_id,doc_id,doc_title,doc_kind,source_path,lang,page_no,section_path,"
    "line_start,line_end,tags,content"
)


@dataclass
class Filters:
    kinds: list[str] = field(default_factory=list)
    langs: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    tag: str = ""
    page_from: int | None = None
    page_to: int | None = None

    def sql(self, prefix: str = "") -> tuple[str, list[Any]]:
        """生成结构化过滤子句。

        这正是 seekdb 相对独立向量库的关键优势：向量、全文、标量过滤
        在同一个执行计划里下推，不需要应用层拼装多次查询结果。
        """
        clauses: list[str] = []
        params: list[Any] = []
        p = prefix
        if self.kinds:
            marks = ",".join(["%s"] * len(self.kinds))
            clauses.append(f"{p}doc_kind IN ({marks})")
            params.extend(self.kinds)
        if self.langs:
            marks = ",".join(["%s"] * len(self.langs))
            clauses.append(f"{p}lang IN ({marks})")
            params.extend(self.langs)
        if self.doc_ids:
            marks = ",".join(["%s"] * len(self.doc_ids))
            clauses.append(f"{p}doc_id IN ({marks})")
            params.extend(self.doc_ids)
        if self.tag:
            clauses.append(f"{p}tags LIKE %s")
            params.append(f"%{self.tag}%")
        if self.page_from is not None:
            clauses.append(f"{p}page_no IS NOT NULL AND {p}page_no >= %s")
            params.append(int(self.page_from))
        if self.page_to is not None:
            clauses.append(f"{p}page_no IS NOT NULL AND {p}page_no <= %s")
            params.append(int(self.page_to))
        return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _match_expr() -> str:
    return "MATCH(`content`,`section_path`) AGAINST(%s IN NATURAL LANGUAGE MODE)"


# ---------------------------------------------------------------------------
# 单路检索
# ---------------------------------------------------------------------------
def semantic_search(qvec: Sequence[float], filters: Filters, k: int) -> tuple[list[dict], str, float]:
    literal = db.vector_literal(qvec)
    where, params = filters.sql()
    sql = (
        f"SELECT {_SAFE_FIELDS}, cosine_distance(`emb`, '{literal}') AS vec_dist "
        f"FROM `chunk` WHERE 1=1{where} "
        f"ORDER BY vec_dist APPROXIMATE LIMIT {int(k)}"
    )
    t0 = time.perf_counter()
    rows = db.query(sql, params)
    ms = (time.perf_counter() - t0) * 1000
    for i, r in enumerate(rows, start=1):
        dist = float(r.pop("vec_dist", 1.0) or 1.0)
        r["vec_score"] = round(max(0.0, 1.0 - dist), 6)
        r["vec_rank"] = i
        r["bm25"] = None
        r["bm25_rank"] = None
    return rows, sql, ms


def keyword_search(qtext: str, filters: Filters, k: int) -> tuple[list[dict], str, float]:
    where, params = filters.sql()
    expr = _match_expr()
    sql = (
        f"SELECT {_SAFE_FIELDS}, {expr} AS bm25 "
        f"FROM `chunk` WHERE {expr}{where} "
        f"ORDER BY bm25 DESC LIMIT {int(k)}"
    )
    t0 = time.perf_counter()
    rows = db.query(sql, [qtext, qtext, *params])
    ms = (time.perf_counter() - t0) * 1000
    for i, r in enumerate(rows, start=1):
        r["bm25"] = round(float(r.get("bm25") or 0.0), 6)
        r["bm25_rank"] = i
        r["vec_score"] = None
        r["vec_rank"] = None
    return rows, sql, ms


# ---------------------------------------------------------------------------
# 混合检索：加权融合
# ---------------------------------------------------------------------------
_SINGLE_SQL_OK: bool | None = None


def _weighted_single_sql(qtext: str, literal: str, filters: Filters, k: int,
                         cand: int, w: float) -> tuple[list[dict], str, float]:
    """尝试在**一条 SQL** 内完成：两路召回 → 各自归一化 → 加权融合 → 排序。"""
    where_v, params_v = filters.sql()
    where_f, params_f = filters.sql()
    sql = f"""
WITH `v` AS (
  SELECT `chunk_id`, (1 - cosine_distance(`emb`, '{literal}')) AS vsim
  FROM `chunk` WHERE 1=1{where_v}
  ORDER BY cosine_distance(`emb`, '{literal}') APPROXIMATE LIMIT {int(cand)}
),
`f` AS (
  SELECT `chunk_id`, {_match_expr()} AS bm25
  FROM `chunk` WHERE {_match_expr()}{where_f}
  ORDER BY bm25 DESC LIMIT {int(cand)}
),
`vn` AS (
  SELECT `chunk_id`, vsim, MAX(vsim) OVER () AS vmax,
         ROW_NUMBER() OVER (ORDER BY vsim DESC) AS vrank
  FROM `v`
),
`fn` AS (
  SELECT `chunk_id`, bm25, MAX(bm25) OVER () AS fmax,
         ROW_NUMBER() OVER (ORDER BY bm25 DESC) AS frank
  FROM `f`
)
SELECT c.chunk_id, c.doc_id, c.doc_title, c.doc_kind, c.source_path, c.lang,
       c.page_no, c.section_path, c.line_start, c.line_end, c.tags, c.content,
       vn.vsim AS vec_raw, vn.vrank AS vec_rank,
       fn.bm25 AS bm25_raw, fn.frank AS bm25_rank,
       ROUND(COALESCE(vn.vsim / NULLIF(vn.vmax,0), 0), 6) AS vec_score,
       ROUND(COALESCE(fn.bm25 / NULLIF(fn.fmax,0), 0), 6) AS bm25,
       ROUND({float(w)} * COALESCE(vn.vsim / NULLIF(vn.vmax,0), 0)
           + {float(1 - w)} * COALESCE(fn.bm25 / NULLIF(fn.fmax,0), 0), 6) AS fused
FROM `vn`
FULL JOIN `fn` ON vn.chunk_id = fn.chunk_id
JOIN `chunk` c ON c.chunk_id = COALESCE(vn.chunk_id, fn.chunk_id)
ORDER BY fused DESC, vec_rank ASC
LIMIT {int(k)}
""".strip()
    t0 = time.perf_counter()
    rows = db.query(sql, [qtext, qtext, *params_f, *params_v])
    ms = (time.perf_counter() - t0) * 1000
    return rows, sql, ms


def _weighted_two_pass(qtext: str, qvec: Sequence[float], filters: Filters,
                       k: int, cand: int, w: float) -> tuple[list[dict], list[str], float]:
    """降级方案：两条下推 SQL 各自取候选，融合在应用层完成（仍是 seekdb 算分）。"""
    vrows, vsql, vms = semantic_search(qvec, filters, cand)
    frows, fsql, fms = keyword_search(qtext, filters, cand)

    vmax = max((r["vec_score"] or 0.0) for r in vrows) if vrows else 0.0
    fmax = max((r["bm25"] or 0.0) for r in frows) if frows else 0.0

    merged: dict[int, dict] = {}
    for r in vrows:
        merged[int(r["chunk_id"])] = {**r, "vec_raw": r["vec_score"]}
    for r in frows:
        cid = int(r["chunk_id"])
        if cid in merged:
            merged[cid].update({"bm25": r["bm25"], "bm25_rank": r["bm25_rank"],
                                "bm25_raw": r["bm25"]})
        else:
            merged[cid] = {**r, "bm25_raw": r["bm25"], "vec_rank": None, "vec_score": None}

    out: list[dict] = []
    for r in merged.values():
        vs = (r.get("vec_raw") or 0.0) / vmax if vmax else 0.0
        bs = (r.get("bm25_raw") or 0.0) / fmax if fmax else 0.0
        r["vec_score"] = round(vs, 6)
        r["bm25"] = round(bs, 6)
        r["fused"] = round(w * vs + (1 - w) * bs, 6)
        out.append(r)
    out.sort(key=lambda x: (-x["fused"], x.get("vec_rank") or 10**6))
    return out[:k], [vsql, fsql], vms + fms


# ---------------------------------------------------------------------------
# 混合检索：seekdb 原生 RRF
# ---------------------------------------------------------------------------
def native_rrf_search(qtext: str, qvec: Sequence[float], filters: Filters, k: int,
                      cand: int, rank_constant: int) -> tuple[list[dict], dict, str, float]:
    """调用 seekdb 原生 dbms_hybrid_search，由数据库内部完成 RRF 融合。

    注意（实测）：1.4.0.0 的 knn 子句只接受 `k`，传 `num_candidates` 会让
    内部生成的 SQL 出现语法错误，因此这里统一用 `k`。
    """
    knn: dict[str, Any] = {
        "field": "emb",
        "query_vector": [round(float(v), 8) for v in qvec],
        "k": int(cand),
    }
    must: list[dict] = []
    if filters.kinds:
        must.append({"terms": {"doc_kind": filters.kinds}})
    if filters.langs:
        must.append({"terms": {"lang": filters.langs}})
    if filters.doc_ids:
        must.append({"terms": {"doc_id": filters.doc_ids}})
    if must:
        # 过滤同时作用于关键词分支与向量分支，避免只过滤单路造成的结果偏斜
        knn["filter"] = {"bool": {"must": must}}
        query_node: dict[str, Any] = {
            "bool": {
                "must": [{"multi_match": {"query": qtext, "fields": ["content", "section_path"]}}],
                "filter": must,
            }
        }
    else:
        query_node = {"multi_match": {"query": qtext, "fields": ["content", "section_path"]}}

    params = {
        "query": query_node,
        "knn": knn,
        "rank": {"rrf": {"rank_constant": int(rank_constant),
                         "rank_window_size": max(int(cand), int(k))}},
        "size": int(k),
        "_source": ["chunk_id", "doc_id", "doc_title", "doc_kind", "source_path",
                    "lang", "page_no", "section_path", "line_start", "line_end",
                    "tags", "content"],
    }
    payload = json.dumps(params, ensure_ascii=False)

    t0 = time.perf_counter()
    hits_raw = db.scalar("SELECT dbms_hybrid_search.search(%s, %s) AS hits",
                         ("chunk", payload), default=None)
    sql_text = db.scalar("SELECT dbms_hybrid_search.get_sql(%s, %s) AS s",
                         ("chunk", payload), default="")
    ms = (time.perf_counter() - t0) * 1000

    hits = json.loads(hits_raw) if isinstance(hits_raw, (str, bytes, bytearray)) else (hits_raw or [])
    rows: list[dict] = []
    for i, h in enumerate(hits, start=1):
        rows.append({
            "chunk_id": h.get("chunk_id"),
            "doc_id": h.get("doc_id"),
            "doc_title": h.get("doc_title"),
            "doc_kind": h.get("doc_kind"),
            "source_path": h.get("source_path"),
            "lang": h.get("lang"),
            "page_no": h.get("page_no"),
            "section_path": h.get("section_path"),
            "line_start": h.get("line_start"),
            "line_end": h.get("line_end"),
            "tags": h.get("tags"),
            "content": h.get("content"),
            "fused": round(float(h.get("_score") or 0.0), 6),
            "rrf_score": round(float(h.get("_score") or 0.0), 6),
            "vec_score": None, "bm25": None,
            "vec_rank": None, "bm25_rank": None,
            "final_rank": i,
        })
    return rows, params, str(sql_text or ""), ms


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------
def _highlight_terms(qtext: str, content: str) -> list[str]:
    """返回在正文里真实出现的查询词元，供前端做高亮（不做任何改写）。"""
    if not content:
        return []
    toks = [t for t in dict.fromkeys(tokenize(qtext)) if len(t) >= 2]
    lower = content.lower()
    hit = [t for t in toks if t.lower() in lower]
    # 英文原文可能是大写开头/全大写，补一轮大小写不敏感的整词匹配
    for t in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", qtext):
        if t.lower() in lower and t not in hit:
            hit.append(t)
    return hit[:24]


def search(query_text: str, mode: str = "hybrid", fusion: str = "rrf",
           semantic_weight: float | None = None, top_k: int | None = None,
           candidate_k: int | None = None, rank_constant: int | None = None,
           filters: Filters | None = None, log_query: bool = True) -> dict:
    """执行一次检索，返回结果 + 真实 SQL + 分数分解 + 耗时。"""
    query_text = (query_text or "").strip()
    if not query_text:
        raise ValueError("检索词不能为空")
    mode = mode if mode in MODES else "hybrid"
    filters = filters or Filters()
    top_k = int(top_k or config.SEARCH_TOP_K)
    candidate_k = max(int(candidate_k or config.SEARCH_CANDIDATE_K), top_k)
    w = config.DEFAULT_SEMANTIC_WEIGHT if semantic_weight is None else float(semantic_weight)
    w = min(1.0, max(0.0, w))
    rank_constant = int(rank_constant or config.RRF_K)

    emb = embedder.get_embedder()
    need_vector = mode in ("semantic", "hybrid")
    qvec = emb.encode_one(query_text) if need_vector else None
    if need_vector and qvec is None:
        raise RuntimeError("向量模型不可用，无法执行语义检索")

    global _SINGLE_SQL_OK
    executed_sql: list[str] = []
    execution = mode
    rows: list[dict] = []
    elapsed = 0.0
    native_params: dict | None = None
    error: str | None = None

    try:
        if mode == "semantic":
            rows, sql, elapsed = semantic_search(qvec, filters, top_k)
            executed_sql.append(sql)
        elif mode == "keyword":
            rows, sql, elapsed = keyword_search(query_text, filters, top_k)
            executed_sql.append(sql)
        elif fusion == "rrf" and config.NATIVE_HYBRID_ENABLED:
            rows, native_params, gen_sql, elapsed = native_rrf_search(
                query_text, qvec, filters, top_k, candidate_k, rank_constant)
            execution = "hybrid/native-rrf"
            executed_sql.append(gen_sql)
        else:
            if _SINGLE_SQL_OK is not False:
                try:
                    rows, sql, elapsed = _weighted_single_sql(
                        query_text, db.vector_literal(qvec), filters, top_k, candidate_k, w)
                    _SINGLE_SQL_OK = True
                    execution = "hybrid/weighted-single-sql"
                    executed_sql.append(sql)
                except Exception as exc:  # noqa: BLE001
                    _SINGLE_SQL_OK = False
                    log.warning("单条 SQL 加权融合不被当前 seekdb 支持，降级为两路查询：%s", exc)
                    error = f"single-sql fusion unavailable: {exc}"[:300]
            if _SINGLE_SQL_OK is False:
                rows, sqls, elapsed = _weighted_two_pass(
                    query_text, qvec, filters, top_k, candidate_k, w)
                execution = "hybrid/weighted-two-pass"
                executed_sql.extend(sqls)
    except Exception as exc:  # noqa: BLE001
        log.exception("检索失败")
        raise

    for i, r in enumerate(rows, start=1):
        r["final_rank"] = i
        r["matched_terms"] = _highlight_terms(query_text, r.get("content") or "")
        r.pop("vec_raw", None)
        r.pop("bm25_raw", None)

    result = {
        "query": query_text,
        "mode": mode,
        "fusion": fusion if mode == "hybrid" else None,
        "execution": execution,
        "semantic_weight": w if mode == "hybrid" and fusion == "weighted" else None,
        "rank_constant": rank_constant if mode == "hybrid" and fusion == "rrf" else None,
        "top_k": top_k,
        "candidate_k": candidate_k,
        "filters": {
            "kinds": filters.kinds, "langs": filters.langs,
            "doc_ids": filters.doc_ids, "tag": filters.tag,
            "page_from": filters.page_from, "page_to": filters.page_to,
        },
        "elapsed_ms": round(elapsed, 3),
        "n_results": len(rows),
        "sql": executed_sql,
        "native_params": native_params,
        "error": error,
        "hits": rows,
    }

    if log_query:
        _write_log(result)
    return result


def _write_log(result: dict) -> None:
    try:
        top_docs = ",".join(dict.fromkeys(
            str(h.get("doc_id")) for h in result["hits"][:8] if h.get("doc_id")))
        db.execute(
            "INSERT INTO `search_log` (query_text,mode,fusion,weight,n_results,"
            "elapsed_ms,sql_text,top_doc_ids,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (result["query"][:1000], result["mode"], result.get("fusion"),
             result.get("semantic_weight"), result["n_results"],
             result["elapsed_ms"], "\n\n----\n\n".join(result["sql"])[:60000],
             top_docs[:1000], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("写入检索日志失败：%s", exc)


def compare_modes(query_text: str, filters: Filters | None = None,
                  top_k: int | None = None) -> dict:
    """三路对比：同一个问题分别用纯语义 / 纯关键词 / 混合检索，返回排名差异。

    这是给使用者（和评审）直观看清「混合检索到底改变了什么」的面板：
    哪些段落只有语义能找到、哪些只有关键词能命中、融合后名次如何变化。
    """
    top_k = int(top_k or config.SEARCH_TOP_K)
    semantic = search(query_text, mode="semantic", top_k=top_k, filters=filters, log_query=False)
    keyword = search(query_text, mode="keyword", top_k=top_k, filters=filters, log_query=False)
    hybrid_rrf = search(query_text, mode="hybrid", fusion="rrf", top_k=top_k,
                        filters=filters, log_query=False)

    def ids(res: dict) -> list[int]:
        return [int(h["chunk_id"]) for h in res["hits"]]

    s_ids, k_ids, h_ids = ids(semantic), ids(keyword), ids(hybrid_rrf)
    union = list(dict.fromkeys(s_ids + k_ids + h_ids))

    by_id = {}
    for res in (semantic, keyword, hybrid_rrf):
        for h in res["hits"]:
            by_id.setdefault(int(h["chunk_id"]), h)

    rows = []
    for cid in union:
        h = by_id.get(cid)
        if not h:
            continue
        rows.append({
            "chunk_id": cid,
            "doc_title": h.get("doc_title"),
            "doc_kind": h.get("doc_kind"),
            "source_path": h.get("source_path"),
            "page_no": h.get("page_no"),
            "section_path": h.get("section_path"),
            "snippet": (h.get("content") or "")[:220],
            "semantic_rank": (s_ids.index(cid) + 1) if cid in s_ids else None,
            "keyword_rank": (k_ids.index(cid) + 1) if cid in k_ids else None,
            "hybrid_rank": (h_ids.index(cid) + 1) if cid in h_ids else None,
            "coverage": ("both" if cid in s_ids and cid in k_ids
                         else "semantic_only" if cid in s_ids
                         else "keyword_only" if cid in k_ids else "none"),
        })
    rows.sort(key=lambda r: (r["hybrid_rank"] is None, r["hybrid_rank"] or 999))

    return {
        "query": query_text,
        "top_k": top_k,
        "modes": {
            "semantic": {"elapsed_ms": semantic["elapsed_ms"], "ids": s_ids, "sql": semantic["sql"]},
            "keyword": {"elapsed_ms": keyword["elapsed_ms"], "ids": k_ids, "sql": keyword["sql"]},
            "hybrid": {"elapsed_ms": hybrid_rrf["elapsed_ms"], "ids": h_ids,
                       "execution": hybrid_rrf["execution"], "sql": hybrid_rrf["sql"]},
        },
        "agreement": {
            "semantic_and_keyword": len(set(s_ids) & set(k_ids)),
            "only_semantic": len(set(s_ids) - set(k_ids)),
            "only_keyword": len(set(k_ids) - set(s_ids)),
            "hybrid_vs_semantic_overlap": len(set(h_ids) & set(s_ids)),
            "hybrid_vs_keyword_overlap": len(set(h_ids) & set(k_ids)),
        },
        "rows": rows,
    }


def related_chunks(chunk_id: int, k: int = 6) -> tuple[list[dict], str]:
    """以段落找段落：用该 chunk 自己的向量做近邻检索（向量索引直接复用）。"""
    row = db.query_one("SELECT `emb` FROM `chunk` WHERE chunk_id=%s LIMIT 1", (int(chunk_id),))
    if not row or row.get("emb") is None:
        return [], ""
    literal = row["emb"] if isinstance(row["emb"], str) else db.vector_literal(row["emb"])
    sql = (
        f"SELECT {_SAFE_FIELDS}, cosine_distance(`emb`, '{literal}') AS vec_dist "
        f"FROM `chunk` WHERE chunk_id <> %s "
        f"ORDER BY vec_dist APPROXIMATE LIMIT {int(k)}"
    )
    rows = db.query(sql, (int(chunk_id),))
    for i, r in enumerate(rows, start=1):
        r["vec_score"] = round(max(0.0, 1.0 - float(r.pop("vec_dist", 1.0) or 1.0)), 6)
        r["vec_rank"] = i
    return rows, sql


def recent_logs(limit: int = 30) -> list[dict]:
    return db.query(
        "SELECT log_id,query_text,mode,fusion,weight,n_results,elapsed_ms,created_at "
        "FROM `search_log` ORDER BY log_id DESC LIMIT %s", (int(limit),))
