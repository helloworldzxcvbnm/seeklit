"""四条检索路径的端到端验证 + 证据采集。

用法：
    PYTHONUTF8=1 python scripts/verify_paths.py [--out logs/verify_paths.txt]

为什么单独写这个脚本：
    1. plink/ssh 的命令行传中文会被本地代码页重编码，检索词必须来自
       代码里的常量（PRESET_QUERIES），不能来自 argv。
    2. 参赛要求「能证明 seekdb 真的在干活」。这里把每一条路径**真实执行**的
       SQL / DSL / 数据库自己生成的 SQL 原样落盘，不做任何改写。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")

from app import config, db, embedder, ingest, schema, search  # noqa: E402
from app.snapshot import PRESET_QUERIES  # noqa: E402

W = 78


class Out:
    """同时写终端和缓冲区，最后整体落盘。"""

    def __init__(self) -> None:
        self.buf = io.StringIO()

    def __call__(self, *parts: str) -> None:
        line = " ".join(str(p) for p in parts)
        print(line)
        self.buf.write(line + "\n")

    def text(self) -> str:
        return self.buf.getvalue()


def rule(o: Out, ch: str = "─") -> None:
    o(ch * W)


def head(o: Out, title: str) -> None:
    o("")
    o("═" * W)
    o(f"  {title}")
    o("═" * W)


def elide_vector(sql: str, keep: int = 24) -> str:
    """把 256 维向量字面量缩成前几维，便于阅读；其余部分原样保留。"""
    out: list[str] = []
    i = 0
    while i < len(sql):
        if sql.startswith("'[", i):
            j = sql.find("]'", i)
            if j == -1:
                out.append(sql[i:])
                break
            body = sql[i + 2:j]
            dims = body.split(",")
            shown = ",".join(d.strip() for d in dims[:keep])
            out.append(f"'[{shown}, … 共 {len(dims)} 维]")
            i = j + 2
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def show_hits(o: Out, rows: list[dict], n: int = 5) -> None:
    for r in rows[:n]:
        fused = r.get("fused")
        vs = r.get("vec_score")
        bm = r.get("bm25")
        bits = []
        if fused is not None:
            bits.append(f"fused={fused}")
        if vs is not None:
            bits.append(f"vec={vs}")
        if bm is not None:
            bits.append(f"bm25={bm}")
        title = (r.get("doc_title") or "")[:28]
        sec = (r.get("section_path") or "")[:26]
        o(f"  #{r.get('final_rank')} [{r.get('doc_kind')}] {title} | {sec} | " + " ".join(bits))


def dump_sql(o: Out, sqls: list[str]) -> None:
    for i, s in enumerate(sqls, start=1):
        if not s:
            o(f"  --- SQL #{i}: (空) ---")
            continue
        o(f"  --- SQL #{i} ---")
        for line in elide_vector(s).splitlines():
            o(f"    {line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/verify_paths.txt")
    ap.add_argument("--query-index", type=int, default=0,
                    help="用 PRESET_QUERIES 里第几条做详细验证")
    args = ap.parse_args()

    o = Out()
    started = datetime.now()

    head(o, "0. 运行环境")
    version = db.server_version()
    o(f"  seekdb 版本        : {version}")
    o(f"  采集时间           : {started:%Y-%m-%d %H:%M:%S}")
    o(f"  连接              : {config.SEEKDB_HOST}:{config.SEEKDB_PORT}/"
      f"{config.SEEKDB_DATABASE}  user={config.SEEKDB_USER}")
    stats = ingest.corpus_stats()
    o(f"  语料              : {stats['docs']} 篇文档 / {stats['chunks']} 段 / "
      f"{stats['chars']:,} 字符")
    emb = embedder.get_embedder()
    o(f"  向量化            : {emb.describe().get('backend', '?')}  "
      f"dim={emb.dim or config.EMBED_DIM}")

    head(o, "1. chunk 表真实结构（SHOW CREATE TABLE）")
    info = schema.table_info()
    if info:
        for line in elide_vector(info[0]["ddl"]).splitlines():
            o(f"  {line}")
    else:
        o("  （表不存在）")

    head(o, "2. 索引清单")
    for r in db.query("SHOW INDEX FROM `chunk`"):
        o(f"  {str(r.get('Key_name')):<14} seq={r.get('Seq_in_index')} "
          f"col={str(r.get('Column_name')):<14} type={r.get('Index_type')}")

    q = PRESET_QUERIES[args.query_index]["text"]
    o("")
    head(o, f"3. 四条路径逐一验证 —— 问题：{q}")

    # ---- 3.1 纯语义 -------------------------------------------------------
    o("")
    rule(o)
    o("  3.1 semantic —— VECTOR 索引 + cosine + ORDER BY ... APPROXIMATE")
    rule(o)
    r = search.search(q, mode="semantic", top_k=5, log_query=False)
    o(f"  execution={r['execution']}  elapsed={r['elapsed_ms']} ms  n={r['n_results']}")
    dump_sql(o, r["sql"])
    show_hits(o, r["hits"])

    # ---- 3.2 纯关键词 -----------------------------------------------------
    o("")
    rule(o)
    o("  3.2 keyword —— FULLTEXT 索引（ik 分词）+ MATCH ... AGAINST → BM25")
    rule(o)
    r = search.search(q, mode="keyword", top_k=5, log_query=False)
    o(f"  execution={r['execution']}  elapsed={r['elapsed_ms']} ms  n={r['n_results']}")
    dump_sql(o, r["sql"])
    show_hits(o, r["hits"])

    # ---- 3.3 原生 RRF ----------------------------------------------------
    o("")
    rule(o)
    o("  3.3 hybrid/rrf —— dbms_hybrid_search.search()，数据库内部完成融合")
    rule(o)
    r = search.search(q, mode="hybrid", fusion="rrf", top_k=5, log_query=False)
    o(f"  execution={r['execution']}  elapsed={r['elapsed_ms']} ms  n={r['n_results']}")
    o("  --- 应用侧提交的 DSL ---")
    dsl = dict(r.get("native_params") or {})
    if "knn" in dsl and "query_vector" in dsl["knn"]:
        dsl = {**dsl, "knn": {**dsl["knn"],
                             "query_vector": f"<{len(dsl['knn']['query_vector'])} 维向量，已省略>"}}
    o("    " + json.dumps(dsl, ensure_ascii=False, indent=2).replace("\n", "\n    "))
    o("  --- 下面是 seekdb 自己生成的 SQL（dbms_hybrid_search.get_sql）---")
    dump_sql(o, r["sql"])
    show_hits(o, r["hits"])

    # ---- 3.4 加权融合 ----------------------------------------------------
    o("")
    rule(o)
    o("  3.4 hybrid/weighted —— 单条 SQL 内两路召回 + 归一化 + 加权")
    rule(o)
    for w in (0.2, 0.6, 0.9):
        r = search.search(q, mode="hybrid", fusion="weighted", semantic_weight=w,
                          top_k=5, log_query=False)
        o(f"  w={w}: execution={r['execution']} elapsed={r['elapsed_ms']} ms "
          f"n={r['n_results']} err={r.get('error')}")
        if w == 0.6:
            dump_sql(o, r["sql"])
            show_hits(o, r["hits"])

    # ---- 3.5 结构化过滤下推 ----------------------------------------------
    o("")
    rule(o)
    o("  3.5 结构化过滤 —— 向量 + 全文 + 标量在同一个执行计划里下推")
    rule(o)
    base = search.search(q, mode="hybrid", fusion="rrf", top_k=8, log_query=False)
    o(f"  无过滤              : n={base['n_results']}")
    for kinds in (["paper"], ["note"], ["record"]):
        f = search.Filters(kinds=kinds)
        rr = search.search(q, mode="hybrid", fusion="rrf", top_k=8, filters=f, log_query=False)
        o(f"  doc_kind={str(kinds):<10}: n={rr['n_results']} elapsed={rr['elapsed_ms']} ms")
    f = search.Filters(langs=["en"])
    rr = search.search(q, mode="hybrid", fusion="rrf", top_k=8, filters=f, log_query=False)
    o(f"  lang=['en']         : n={rr['n_results']}")
    o("  --- 过滤下推体现在 DSL 的 filter 子句里 ---")
    o("    " + json.dumps((rr.get("native_params") or {}), ensure_ascii=False)[:600])

    # ---- 4. 三路对比 ------------------------------------------------------
    head(o, "4. compare_modes —— 三路排名差异")
    cmp = search.compare_modes(q, top_k=8)
    o(f"  agreement: {json.dumps(cmp['agreement'], ensure_ascii=False)}")
    o("")
    o(f"  {'语义':>6} {'关键词':>6} {'混合':>6}  {'覆盖':<14} 文档 / 段落")
    for row in cmp["rows"]:
        s = row["semantic_rank"] if row["semantic_rank"] else "-"
        k = row["keyword_rank"] if row["keyword_rank"] else "-"
        h = row["hybrid_rank"] if row["hybrid_rank"] else "-"
        o(f"  {str(s):>6} {str(k):>6} {str(h):>6}  {row['coverage']:<14} "
          f"{(row['doc_title'] or '')[:20]} / {(row['section_path'] or '')[:22]}")
    o("")
    o("  --- 纯语义路径 SQL ---")
    dump_sql(o, cmp["modes"]["semantic"]["sql"])
    o("  --- 纯关键词路径 SQL ---")
    dump_sql(o, cmp["modes"]["keyword"]["sql"])

    # ---- 5. 以段找段 ------------------------------------------------------
    head(o, "5. related_chunks —— 复用向量索引做段落到段落近邻")
    top = search.search(q, mode="hybrid", fusion="rrf", top_k=1, log_query=False)["hits"][0]
    cid = int(top["chunk_id"])
    rows, sql = search.related_chunks(cid, k=5)
    o(f"  源 chunk_id={cid}  →  {len(rows)} 个近邻")
    dump_sql(o, [sql])
    for i, r in enumerate(rows, start=1):
        o(f"  #{i} vec_score={r['vec_score']}  [{r['doc_kind']}] "
          f"{(r.get('doc_title') or '')[:24]} | {(r.get('section_path') or '')[:26]}")

    # ---- 6. 全预设批量 ----------------------------------------------------
    head(o, "6. 全部预设问题 × 四条路径")
    o(f"  {'问题':<34} {'语义':>7} {'关键词':>7} {'原生RRF':>8} {'加权':>7}")
    totals = {"semantic": 0.0, "keyword": 0.0, "rrf": 0.0, "weighted": 0.0}
    counts = {"semantic": 0, "keyword": 0, "rrf": 0, "weighted": 0}
    for item in PRESET_QUERIES:
        t = item["text"]
        ms = {}
        ns = {}
        for key, kw in (("semantic", dict(mode="semantic")),
                        ("keyword", dict(mode="keyword")),
                        ("rrf", dict(mode="hybrid", fusion="rrf")),
                        ("weighted", dict(mode="hybrid", fusion="weighted"))):
            t0 = time.perf_counter()
            rr = search.search(t, top_k=8, log_query=False, **kw)
            ms[key] = (time.perf_counter() - t0) * 1000
            ns[key] = rr["n_results"]
            totals[key] += rr["elapsed_ms"]
            counts[key] += 1
        label = t if len(t) <= 32 else t[:31] + "…"
        o(f"  {label:<34} {ms['semantic']:>6.1f}m {ms['keyword']:>6.1f}m "
          f"{ms['rrf']:>7.1f}m {ms['weighted']:>6.1f}m   "
          f"n={ns['semantic']}/{ns['keyword']}/{ns['rrf']}/{ns['weighted']}")
    o("")
    o("  seekdb 侧平均耗时（不含向量化）：")
    for k in ("semantic", "keyword", "rrf", "weighted"):
        o(f"    {k:<10} {totals[k] / max(counts[k], 1):>8.2f} ms")

    # ---- 7. 检索日志 ------------------------------------------------------
    head(o, "7. search_log —— 每次检索都留痕")
    logs = search.recent_logs(limit=5)
    o(f"  最近 {len(logs)} 条：")
    for lg in logs:
        o(f"    [{lg.get('created_at')}] {lg.get('mode')}/{lg.get('fusion')} "
          f"n={lg.get('n_results')} {lg.get('elapsed_ms')}ms  {(lg.get('query_text') or '')[:30]}")
    total = db.scalar("SELECT COUNT(*) FROM `search_log`", default=0)
    o(f"  search_log 累计 {total} 条")

    o("")
    o("═" * W)
    o(f"  采集完成，用时 {(datetime.now() - started).total_seconds():.1f} s")
    o("═" * W)

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(o.text())
    print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
