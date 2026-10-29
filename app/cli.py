"""溯文 SeekLit —— 命令行入口。

不开 Web 也能跑完整链路，这一点对「可复现」很重要：
评审可以直接在服务器上用命令验证 seekdb 真的在干活。

    python -m app.cli info                      # 打印 seekdb 版本、表结构、语料规模
    python -m app.cli migrate                   # 在线补齐缺失的全文索引
    python -m app.cli preview                   # 扫描语料目录，列出待导入文件
    python -m app.cli ingest --mode rebuild     # 全量重建索引
    python -m app.cli ingest --mode incremental # 只导入新文件
    python -m app.cli search "持续小幅慢漂移用哪种方法在线检测"
    python -m app.cli search "CUSUM k h 标定" --mode keyword
    python -m app.cli search "毛刺" --mode hybrid --fusion weighted --weight 0.35 --show-sql
    python -m app.cli compare "报警拖尾导致误判故障仍在持续"
    python -m app.cli related 1001              # 以段找段
    python -m app.cli eval                      # 跑一遍预设问题，输出效果数据
    python -m app.cli snapshot                  # 导出检索结果快照 JSON（供公开站点离线演示）
    python -m app.cli serve                     # 启动 HTTP 服务
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import config, db, embedder, ingest, schema, search
from .snapshot import PRESET_QUERIES, build_snapshot, to_json


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------
class C:
    """终端颜色，非 TTY 时自动关闭。"""
    _on = sys.stdout.isatty()

    @staticmethod
    def _w(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if C._on else text

    @classmethod
    def b(cls, t: str) -> str: return cls._w("1", t)
    @classmethod
    def dim(cls, t: str) -> str: return cls._w("2", t)
    @classmethod
    def blue(cls, t: str) -> str: return cls._w("34", t)
    @classmethod
    def green(cls, t: str) -> str: return cls._w("32", t)
    @classmethod
    def amber(cls, t: str) -> str: return cls._w("33", t)
    @classmethod
    def red(cls, t: str) -> str: return cls._w("31", t)
    @classmethod
    def cyan(cls, t: str) -> str: return cls._w("36", t)


def hr(ch: str = "─", n: int = 78) -> None:
    print(C.dim(ch * n))


def head(title: str) -> None:
    print()
    hr("═")
    print(C.b(f"  {title}"))
    hr("═")


def kv(pairs: list[tuple[str, Any]]) -> None:
    if not pairs:
        return
    width = max(len(str(k)) for k, _ in pairs)
    for k, v in pairs:
        print(f"  {C.dim(str(k).ljust(width))}  {v}")


def as_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def fix_argv_encoding() -> None:
    """把 argv 里的孤立代理项还原成正常 UTF-8。

    服务器常见 LANG=C / POSIX，此时 CPython 用 ASCII + surrogateescape 解码
    命令行参数，中文检索词会变成 U+DC80 这类孤立代理项。它们能正常参与
    Python 内部字符串运算，但一旦拼进 SQL 交给 pymysql 做 .encode('utf-8')
    就会抛 "UnicodeEncodeError: surrogates not allowed"，报错信息还看不出
    是编码问题。这里在入口处一次性修正。

    等价的临时办法是 `PYTHONUTF8=1 python -m app.cli ...`。
    """
    fixed = []
    changed = False
    for arg in sys.argv:
        try:
            arg.encode("utf-8")
            fixed.append(arg)
        except UnicodeEncodeError:
            fixed.append(arg.encode("utf-8", "surrogateescape").decode("utf-8", "replace"))
            changed = True
    if changed:
        sys.argv = fixed
        print(C.amber("  提示：当前 locale 不是 UTF-8，已自动修正命令行里的中文参数。"
                      "建议 export PYTHONUTF8=1 以获得一致行为。"), file=sys.stderr)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_info(_args) -> int:
    head("运行环境")
    version = db.server_version()
    ok = not version.startswith("unreachable")
    kv([
        ("seekdb", (C.green(version) if ok else C.red(version))),
        ("连接", f"{config.SEEKDB_HOST}:{config.SEEKDB_PORT}/{config.SEEKDB_DATABASE}  user={config.SEEKDB_USER}"),
        ("向量化", embedder.get_embedder().describe().get("backend", "?")),
    ])
    if not ok:
        print(C.red("\n  seekdb 不可达，请先执行 scripts/start_seekdb.sh"))
        return 1

    head("语料规模")
    try:
        c = ingest.corpus_stats()
    except Exception as exc:  # noqa: BLE001
        # 首次运行、还没建表时属于正常情况，不应该报错退出
        print(C.amber("  表还没创建，先跑一次导入即可。"))
        print(C.dim(f"  （{type(exc).__name__}: {exc}）"))
        head("下一步")
        print("  python -m app.cli preview                 # 看看语料目录里有什么")
        print("  python -m app.cli ingest --mode rebuild   # 建表 + 拟合向量 + 写入索引")
        return 0
    kv([
        ("文档", c["docs"]),
        ("分块", c["chunks"]),
        ("字符", f"{c['chars']:,}"),
        ("按类型", "  ".join(f"{r['doc_kind']}:{r['docs']}篇/{r['chunks']}段" for r in c["by_kind"]) or "—"),
        ("按语言", "  ".join(f"{r['lang']}:{r['chunks']}段" for r in c["by_lang"]) or "—"),
    ])

    head("chunk 表真实结构（SHOW CREATE TABLE）")
    info = schema.table_info()
    if info:
        print(C.cyan(info[0]["ddl"]))
    else:
        print(C.amber("  表尚未创建，先跑 python -m app.cli ingest --mode rebuild"))

    fts = schema.existing_fulltext_indexes("chunk")
    if fts:
        head("全文索引清单")
        for name, cols in fts.items():
            expect = schema._REQUIRED_FULLTEXT.get(name)
            flag = ""
            if expect is None:
                flag = C.amber("  (非本项目预期索引)")
            elif expect[0] != cols:
                flag = C.amber(f"  (列清单不符，预期 {', '.join(expect[0])})")
            # 先按纯文本对齐再上色，否则 ANSI 转义会被算进宽度
            print(f"  {C.green(name.ljust(12))} ({', '.join(cols)}){flag}")
        missing = [n for n in schema._REQUIRED_FULLTEXT if n not in fts]
        if missing:
            print(C.amber(f"\n  缺失：{', '.join(missing)}"))
            print(C.amber("  → dbms_hybrid_search 的全文分支会报 1191，"
                          "请执行 python -m app.cli migrate"))
    return 0


def cmd_migrate(_args) -> int:
    """在线补齐缺失的全文索引，不需要重灌数据。

    老版本的 chunk 表只有复合索引 ft_chunk(content, section_path)，
    而 dbms_hybrid_search 生成的是单列 match(`content`)，列清单对不上就 1191。
    """
    head("schema 迁移")
    if not schema.has_table("chunk"):
        print(C.amber("  chunk 表还不存在，无需迁移；请先执行 ingest。"))
        return 0
    before = schema.existing_fulltext_indexes("chunk")
    print("  迁移前：" + (", ".join(f"{k}({','.join(v)})" for k, v in before.items()) or "无全文索引"))
    result = schema.ensure_fulltext_indexes("chunk")
    after = schema.existing_fulltext_indexes("chunk")
    print("  迁移后：" + ", ".join(f"{k}({','.join(v)})" for k, v in after.items()))
    kv([
        ("新增", ", ".join(result["added"]) or "—"),
        ("重建", ", ".join(result["rebuilt"]) or "—"),
        ("已存在跳过", ", ".join(result["skipped"]) or "—"),
    ])
    print(C.green("\n  迁移完成。原生混合检索（dbms_hybrid_search）现在可以走全文分支了。"))
    return 0


def cmd_preview(_args) -> int:
    entries = ingest.scan_corpus()
    known = ingest.existing_hashes()
    head(f"语料目录 {config.CORPUS_DIR}")
    print(f"  {'状态':<6}{'类型':<8}{'大小':>10}   文件")
    hr()
    for e in entries:
        flag = C.dim("已导入") if e.sha256 in known else C.amber("待导入")
        size = f"{e.size / 1024:.1f}KB"
        print(f"  {flag:<6}{e.doc_kind:<8}{size:>10}   {e.rel}")
    hr()
    new = sum(1 for e in entries if e.sha256 not in known)
    print(f"  共 {len(entries)} 个文件，其中 {new} 个待导入，"
          f"总计 {sum(e.size for e in entries) / 1024 / 1024:.2f} MB")
    return 0


def cmd_ingest(args) -> int:
    head(f"导入语料（mode={args.mode}）")
    t0 = time.perf_counter()

    def progress(msg: str) -> None:
        print(f"  {C.dim(time.strftime('%H:%M:%S'))} {msg}", flush=True)

    report = ingest.run_import(mode=args.mode, limit=args.limit, progress=progress)
    wall = time.perf_counter() - t0
    d = report.as_dict()

    head("导入报告")
    emb = d.get("embedder") or {}
    kv([
        ("文件 导入/跳过/总计", f"{d['files_imported']} / {d['files_skipped']} / {d['files_total']}"),
        ("分块", d["chunks"]),
        ("字符", f"{d['chars']:,}"),
        ("向量维度", f"{d['dim']} d"),
        ("向量化后端", f"{emb.get('backend', '?')} · {emb.get('method', '')}".strip(" ·")),
        ("词表规模", f"{emb.get('vocab_size', '—')}"),
        ("解析耗时", f"{d['timings']['parse_s']} s"),
        ("向量化耗时", f"{d['timings']['embed_s']} s"),
        ("写入 seekdb", f"{d['timings']['db_s']} s"),
        ("合计（内部计时）", f"{d['timings']['total_s']} s"),
        ("墙钟", f"{wall:.1f} s"),
    ])

    if report.per_file:
        head("逐文件明细")
        print(f"  {'分块':>5} {'字符':>8} {'解析s':>7}  {'语言':<5}文件")
        hr()
        for f in report.per_file:
            print(f"  {f.get('chunks', 0):>5} {f.get('chars', 0):>8} "
                  f"{float(f.get('parse_seconds') or 0):>7.2f}  "
                  f"{(f.get('lang') or '—'):<5}{f.get('file') or ''}")

    if report.errors:
        head("错误")
        for e in report.errors:
            print(C.red(f"  - {e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)}"))
        return 1
    print(C.green("\n  导入完成。"))
    return 0


def _print_hits(res: dict, show_sql: bool, full: bool) -> None:
    mode_label = {"hybrid": "混合", "semantic": "语义", "keyword": "关键词"}[res["mode"]]
    head(f"检索：{res['query']}")
    kv([
        ("模式", f"{mode_label}" + (f" / {res['fusion']}" if res.get("fusion") else "")),
        ("执行方式", C.cyan(res["execution"])),
        ("耗时", f"{res['elapsed_ms']} ms"),
        ("结果", f"{res['n_results']} 段（候选池 {res['candidate_k']}）"),
    ] + ([("语义权重", res["semantic_weight"])] if res.get("semantic_weight") is not None else [])
      + ([("rank_constant", res["rank_constant"])] if res.get("rank_constant") is not None else []))
    if res.get("error"):
        print(C.amber(f"  注意：{res['error']}"))

    filters = res.get("filters") or {}
    active = [f"{k}={v}" for k, v in filters.items() if v not in (None, "", [], {})]
    if active:
        print(f"  {C.dim('过滤条件')}  {'  '.join(active)}")

    print()
    for h in res["hits"]:
        fused = h.get("fused", h.get("rrf_score"))
        bits = []
        if fused is not None:
            bits.append(f"融合={float(fused):.4f}")
        if h.get("vec_score") is not None:
            bits.append(f"语义={float(h['vec_score']):.4f}" + (f"(r{h['vec_rank']})" if h.get("vec_rank") else ""))
        if h.get("bm25") is not None:
            bits.append(f"BM25={float(h['bm25']):.4f}" + (f"(r{h['bm25_rank']})" if h.get("bm25_rank") else ""))
        loc = []
        if h.get("section_path"):
            loc.append(h["section_path"])
        if h.get("page_no"):
            loc.append(f"第{h['page_no']}页")
        if h.get("line_start"):
            loc.append(f"行{h['line_start']}-{h.get('line_end') or h['line_start']}")
        print(f"  {C.b('#' + str(h['final_rank']))}"
              f" {C.blue(h.get('doc_title') or h.get('source_path') or '')}"
              f" {C.dim('[' + (h.get('doc_kind') or '?') + ']')}")
        if loc:
            print(f"     {C.dim(' · '.join(loc))}")
        print(f"     {C.amber(' | '.join(bits))}")
        text = (h.get("content") or "").replace("\n", " ")
        if not full and len(text) > 300:
            text = text[:300] + "…"
        print(f"     {text}")
        if h.get("matched_terms"):
            print(f"     {C.dim('命中词元: ' + ', '.join(h['matched_terms']))}")
        print(f"     {C.dim('chunk_id=' + str(h['chunk_id']) + '  ' + str(h.get('source_path', '')))}")
        print()

    if show_sql:
        head("本次实际执行的 SQL")
        for i, s in enumerate(res["sql"], 1):
            if not s:
                continue
            print(C.dim(f"  -- [{i}]"))
            print(C.cyan(s))
            print()
        if res.get("native_params"):
            head("dbms_hybrid_search.search() 的入参 DSL")
            print(C.cyan(as_json(res["native_params"])))
            print(C.dim("\n  ↑ 上面的 SQL 由 seekdb 收到这段 DSL 后自己生成，用 get_sql() 原样取回。"))


def cmd_search(args) -> int:
    filters = search.Filters(
        kinds=[k for k in (args.kind or "").split(",") if k],
        langs=[x for x in (args.lang or "").split(",") if x],
        tag=args.tag or "",
        page_from=args.page_from, page_to=args.page_to,
    )
    res = search.search(
        query_text=args.query, mode=args.mode, fusion=args.fusion,
        semantic_weight=args.weight, top_k=args.top_k, candidate_k=args.candidate_k,
        rank_constant=args.rank_constant, filters=filters, log_query=not args.no_log,
    )
    if args.json:
        print(as_json(res))
    else:
        _print_hits(res, args.show_sql, args.full)
    return 0


def cmd_compare(args) -> int:
    filters = search.Filters(
        kinds=[k for k in (args.kind or "").split(",") if k],
        langs=[x for x in (args.lang or "").split(",") if x],
    )
    res = search.compare_modes(args.query, filters, args.top_k)
    if args.json:
        print(as_json(res))
        return 0

    head(f"三路对比：{args.query}")
    a = res["agreement"]
    kv([
        ("两路都召回", C.green(a["semantic_and_keyword"])),
        ("只有语义找到", C.blue(a["only_semantic"])),
        ("只有关键词命中", C.amber(a["only_keyword"])),
        ("混合 ∩ 语义", a["hybrid_vs_semantic_overlap"]),
        ("混合 ∩ 关键词", a["hybrid_vs_keyword_overlap"]),
    ])
    m = res["modes"]
    print(f"\n  {C.dim('耗时')}  语义 {m['semantic']['elapsed_ms']} ms   "
          f"关键词 {m['keyword']['elapsed_ms']} ms   混合 {m['hybrid']['elapsed_ms']} ms "
          f"({C.cyan(m['hybrid'].get('execution', ''))})")

    head("排名对照")
    print(f"  {'语义':>5}{'关键词':>7}{'混合':>6}   {'召回来源':<12} 段落")
    hr()
    cover = {"both": "两路共有", "semantic_only": "仅语义", "keyword_only": "仅关键词", "none": "仅混合"}
    for r in res["rows"]:
        s = str(r["semantic_rank"]) if r["semantic_rank"] else C.dim("—")
        k = str(r["keyword_rank"]) if r["keyword_rank"] else C.dim("—")
        h = C.b(str(r["hybrid_rank"])) if r["hybrid_rank"] else C.dim("—")
        src = cover.get(r["coverage"], r["coverage"])
        snip = (r["snippet"] or "").replace("\n", " ")[:52]
        print(f"  {s:>5}{k:>7}{h:>6}   {src:<12} {snip}")
    if args.show_sql:
        head("三路各自的 SQL")
        for name in ("semantic", "keyword", "hybrid"):
            print(C.dim(f"  -- {name}"))
            for s in m[name]["sql"]:
                if s:
                    print(C.cyan(s))
            print()
    return 0


def cmd_related(args) -> int:
    rows, sql = search.related_chunks(args.chunk_id, args.k)
    head(f"以段找段：chunk_id={args.chunk_id}")
    if not rows:
        print(C.amber("  没有找到（该分块可能不存在或没有向量）"))
        return 1
    for i, r in enumerate(rows, 1):
        cos = float(r.get("vec_score") or 0.0)
        print(f"  {C.b('#' + str(i))} {C.blue(r.get('doc_title') or '')} "
              f"{C.amber('cos=' + format(cos, '.4f'))}")
        loc = " · ".join(x for x in [r.get("section_path"),
                                     f"第{r['page_no']}页" if r.get("page_no") else None] if x)
        if loc:
            print(f"     {C.dim(loc)}")
        snippet = (r.get("content") or "")[:180].replace("\n", " ")
        print(f"     {snippet}")
        print()
    if args.show_sql and sql:
        head("近邻 SQL")
        print(C.cyan(sql))
    return 0


def cmd_eval(args) -> int:
    """把预设问题全部跑一遍，产出可以直接写进报告的数字。"""
    queries = [q["text"] for q in PRESET_QUERIES]
    head(f"效果验证：{len(queries)} 个真实问题 × 3 条检索路径")
    print(C.dim("  说明：本项目不做人工标注，因此不报 precision/recall；"
                "下面给的是每条路径的可观测指标 —— 召回量、两路重合度、耗时、"
                "以及混合相对单路带来的排名变化。所有数字均来自 seekdb 真实查询。"))
    print()

    totals = {"semantic_ms": 0.0, "keyword_ms": 0.0, "hybrid_ms": 0.0,
              "both": 0, "only_sem": 0, "only_kw": 0, "moved": 0, "n": 0}
    rows_out = []
    print(f"  {'问题':<34}{'语义':>6}{'关键词':>8}{'混合':>6}{'两路共有':>10}{'仅语义':>8}{'仅关键词':>9}{'排名变动':>9}")
    hr()
    for q in queries:
        try:
            res = search.compare_modes(q, search.Filters(), args.top_k)
        except Exception as exc:  # noqa: BLE001
            print(C.red(f"  {q[:32]:<34} 失败：{exc}"))
            continue
        a = res["agreement"]
        m = res["modes"]
        moved = sum(1 for r in res["rows"]
                    if r["hybrid_rank"] and r["semantic_rank"] and r["hybrid_rank"] != r["semantic_rank"])
        print(f"  {q[:32]:<34}{len(m['semantic']['ids']):>6}{len(m['keyword']['ids']):>8}"
              f"{len(m['hybrid']['ids']):>6}{a['semantic_and_keyword']:>10}"
              f"{a['only_semantic']:>8}{a['only_keyword']:>9}{moved:>9}")
        totals["semantic_ms"] += m["semantic"]["elapsed_ms"]
        totals["keyword_ms"] += m["keyword"]["elapsed_ms"]
        totals["hybrid_ms"] += m["hybrid"]["elapsed_ms"]
        totals["both"] += a["semantic_and_keyword"]
        totals["only_sem"] += a["only_semantic"]
        totals["only_kw"] += a["only_keyword"]
        totals["moved"] += moved
        totals["n"] += 1
        rows_out.append({"query": q, "agreement": a, "moved": moved,
                         "elapsed_ms": {k: m[k]["elapsed_ms"] for k in m}})

    n = max(totals["n"], 1)
    hr()
    head("汇总")
    kv([
        ("问题数", totals["n"]),
        ("平均耗时 · 语义", f"{totals['semantic_ms'] / n:.1f} ms"),
        ("平均耗时 · 关键词", f"{totals['keyword_ms'] / n:.1f} ms"),
        ("平均耗时 · 混合(原生RRF)", f"{totals['hybrid_ms'] / n:.1f} ms"),
        ("两路共有（平均）", f"{totals['both'] / n:.2f}"),
        ("仅语义找到（平均）", C.blue(f"{totals['only_sem'] / n:.2f}")),
        ("仅关键词命中（平均）", C.amber(f"{totals['only_kw'] / n:.2f}")),
        ("混合后排名变动（平均）", f"{totals['moved'] / n:.2f}"),
    ])
    print(C.dim("\n  「仅语义」+「仅关键词」> 0 就是混合检索的价值所在："))
    print(C.dim("  任何一条单路都会漏掉另一条能找到的段落，融合把两部分都保住。"))

    if args.json:
        print()
        print(as_json({"summary": {k: (v / n if isinstance(v, float) or k.endswith('_ms') else v)
                                   for k, v in totals.items()}, "rows": rows_out}))
    return 0


def cmd_snapshot(args) -> int:
    head("导出检索结果快照")
    print(C.dim("  快照里每个分数都来自服务器上的真实 seekdb 查询；"))
    print(C.dim("  公开站点直接读这份 JSON 做离线交互演示，不需要暴露任何服务端口。"))
    t0 = time.perf_counter()
    snap = build_snapshot(
        queries=[q for q in (args.queries.split("|") if args.queries else []) if q],
        top_k=args.top_k, candidate_k=args.candidate_k,
        weights=[float(w) for w in args.weights.split(",") if w != ""],
    )
    out = config.SNAPSHOT_DIR / "seeklit_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 紧凑分隔符：这份 JSON 是给站点程序读的，不是给人读的，
    # indent=2 会把体积撑大 40% 左右，静态站点上没必要。
    # Decimal/bytes 的兜底转换统一在 snapshot.to_json 里做。
    out.write_text(to_json(snap, compact=True), encoding="utf-8")
    head("导出完成")
    kv([
        ("文件", str(out)),
        ("大小", f"{out.stat().st_size / 1024:.1f} KB"),
        ("问题数", len(snap.get("queries", []))),
        ("候选池/问题", args.candidate_k),
        ("耗时", f"{time.perf_counter() - t0:.1f} s"),
    ])
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.host, args.port)
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="溯文 SeekLit —— 基于 OceanBase seekdb 的科研文献与笔记混合检索",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="打印 seekdb 版本、语料规模与 chunk 表结构").set_defaults(fn=cmd_info)
    sub.add_parser("migrate", help="在线补齐 chunk 表缺失的全文索引（不重灌数据）").set_defaults(fn=cmd_migrate)
    sub.add_parser("preview", help="扫描语料目录").set_defaults(fn=cmd_preview)

    sp = sub.add_parser("ingest", help="导入语料并建立索引")
    sp.add_argument("--mode", choices=["rebuild", "incremental"], default="rebuild")
    sp.add_argument("--limit", type=int, default=None, help="只处理前 N 个文件，用于快速验证")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("search", help="执行一次检索")
    sp.add_argument("query")
    sp.add_argument("--mode", choices=list(search.MODES), default="hybrid")
    sp.add_argument("--fusion", choices=list(search.FUSIONS), default="rrf")
    sp.add_argument("--weight", type=float, default=None, help="语义权重（fusion=weighted 时生效）")
    sp.add_argument("--top-k", type=int, default=None, dest="top_k")
    sp.add_argument("--candidate-k", type=int, default=None, dest="candidate_k")
    sp.add_argument("--rank-constant", type=int, default=None, dest="rank_constant")
    sp.add_argument("--kind", default="", help="逗号分隔：paper,note,record")
    sp.add_argument("--lang", default="", help="逗号分隔：zh,en")
    sp.add_argument("--tag", default="")
    sp.add_argument("--page-from", type=int, default=None, dest="page_from")
    sp.add_argument("--page-to", type=int, default=None, dest="page_to")
    sp.add_argument("--show-sql", action="store_true", dest="show_sql", help="打印实际执行的 SQL")
    sp.add_argument("--full", action="store_true", help="不截断正文")
    sp.add_argument("--json", action="store_true", help="输出原始 JSON")
    sp.add_argument("--no-log", action="store_true", dest="no_log", help="不写入 search_log")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("compare", help="三路对比：纯语义 / 纯关键词 / 混合")
    sp.add_argument("query")
    sp.add_argument("--top-k", type=int, default=None, dest="top_k")
    sp.add_argument("--kind", default="")
    sp.add_argument("--lang", default="")
    sp.add_argument("--show-sql", action="store_true", dest="show_sql")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_compare)

    sp = sub.add_parser("related", help="以段找段")
    sp.add_argument("chunk_id", type=int)
    sp.add_argument("--k", type=int, default=6)
    sp.add_argument("--show-sql", action="store_true", dest="show_sql")
    sp.set_defaults(fn=cmd_related)

    sp = sub.add_parser("eval", help="跑一遍预设问题，输出效果数据")
    sp.add_argument("--top-k", type=int, default=8, dest="top_k")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("snapshot", help="导出检索结果快照 JSON")
    sp.add_argument("--queries", default="", help="用 | 分隔的自定义问题，留空则用内置预设")
    sp.add_argument("--top-k", type=int, default=10, dest="top_k")
    sp.add_argument("--candidate-k", type=int, default=40, dest="candidate_k")
    sp.add_argument("--weights", default="0,0.3,0.5,0.7,1.0")
    sp.set_defaults(fn=cmd_snapshot)

    sp = sub.add_parser("serve", help="启动 HTTP 服务")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        fix_argv_encoding()
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print(C.amber("\n已中断"))
        return 130
    except Exception as exc:  # noqa: BLE001
        print(C.red(f"\n错误：{type(exc).__name__}: {exc}"))
        if "--traceback" in sys.argv or config.DEBUG:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
