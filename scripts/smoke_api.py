"""HTTP API 冒烟测试：把每个端点都真打一遍，检查状态码、响应结构与耗时。

为什么要用文件里的常量当检索词：
    ssh/plink 传中文命令行参数会被本地代码页重编码（实测变成 U+FFFD），
    所以查询词一律取自 app.snapshot.PRESET_QUERIES，不走 argv。

用法：
    PYTHONUTF8=1 python scripts/smoke_api.py [--base http://127.0.0.1:8090]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, ".")

from app.snapshot import PRESET_QUERIES  # noqa: E402

PASS, FAIL = 0, 0
NOTES: list[str] = []


def call(base: str, method: str, path: str, body: dict | None = None,
         timeout: int = 60) -> tuple[int, object, float]:
    url = base.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ms = (time.perf_counter() - t0) * 1000
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode("utf-8")), ms
            return resp.status, raw, ms
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        ms = (time.perf_counter() - t0) * 1000
        try:
            return exc.code, json.loads(raw.decode("utf-8")), ms
        except Exception:  # noqa: BLE001
            return exc.code, raw[:400], ms
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}", (time.perf_counter() - t0) * 1000


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}{(' — ' + detail) if detail else ''}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{(' — ' + detail) if detail else ''}")
        NOTES.append(f"{name}: {detail}")
    return ok


def section(title: str) -> None:
    print()
    print("═" * 78)
    print(f"  {title}")
    print("═" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    args = ap.parse_args()
    base = args.base

    section(f"GET /api/health   ({base})")
    st, body, ms = call(base, "GET", "/api/health")
    check("health 200", st == 200, f"{ms:.0f} ms")
    if st == 200 and isinstance(body, dict):
        check("seekdb_reachable", body.get("seekdb_reachable") is True,
              str(body.get("seekdb_version")))
        corpus = body.get("corpus") or {}
        check("corpus 非空", int(corpus.get("chunks") or 0) > 0,
              f"{corpus.get('docs')} docs / {corpus.get('chunks')} chunks")
        check("chunk_table_ddl 含 VECTOR 索引", "VECTOR KEY" in str(body.get("chunk_table_ddl", "")))
        check("chunk_table_ddl 含 FULLTEXT 索引",
              str(body.get("chunk_table_ddl", "")).count("FULLTEXT KEY") == 3,
              "期望 3 个：ft_content / ft_section / ft_chunk")

    section("GET /api/stats")
    st, body, ms = call(base, "GET", "/api/stats")
    check("stats 200", st == 200, f"{ms:.0f} ms")
    if isinstance(body, dict):
        print("    " + json.dumps(body, ensure_ascii=False)[:500])

    section("GET /api/docs（接口清单）")
    st, body, ms = call(base, "GET", "/api/docs")
    check("docs 200", st == 200, f"{ms:.0f} ms")
    if isinstance(body, dict):
        eps = body.get("endpoints") or []
        check("端点数 > 8", len(eps) > 8, f"{len(eps)} 条")
        print(f"    runtime: {body.get('runtime')}")
        for e in eps:
            print(f"      {e.get('method'):<7} {e.get('path'):<34} "
                  f"{'[auth]' if e.get('auth') else '      '} {e.get('summary', '')[:40]}")

    section("GET /api/preset-queries")
    st, body, ms = call(base, "GET", "/api/preset-queries")
    check("preset-queries 200", st == 200, f"{ms:.0f} ms")
    presets = body.get("queries") if isinstance(body, dict) else body
    check("预设问题非空", bool(presets), f"{len(presets or [])} 条")

    section("GET /api/documents")
    st, body, ms = call(base, "GET", "/api/documents")
    check("documents 200", st == 200, f"{ms:.0f} ms")
    docs = body.get("documents") if isinstance(body, dict) else body
    check("文档数 = 26", len(docs or []) == 26, f"实际 {len(docs or [])}")
    first_doc_id = (docs or [{}])[0].get("doc_id") if docs else None

    section("POST /api/search —— 四条执行路径")
    cases = [
        ("semantic", dict(mode="semantic")),
        ("keyword", dict(mode="keyword")),
        ("hybrid/rrf", dict(mode="hybrid", fusion="rrf")),
        ("hybrid/weighted", dict(mode="hybrid", fusion="weighted", semantic_weight=0.6)),
    ]
    q = PRESET_QUERIES[0]["text"]
    for label, payload in cases:
        st, body, ms = call(base, "POST", "/api/search", {"query": q, "top_k": 5, **payload})
        ok = st == 200 and isinstance(body, dict) and body.get("n_results", 0) > 0
        detail = ""
        if isinstance(body, dict):
            detail = (f"execution={body.get('execution')} n={body.get('n_results')} "
                      f"db={body.get('elapsed_ms')}ms wall={ms:.0f}ms err={body.get('error')}")
        else:
            detail = f"status={st} body={str(body)[:200]}"
        check(f"search {label}", ok, detail)
        if isinstance(body, dict) and body.get("sql"):
            print(f"        真实执行 SQL {len(body['sql'])} 条，首条前 160 字符：")
            print(f"        {str(body['sql'][0])[:160]}…")

    section("POST /api/search —— 结构化过滤下推")
    for kinds in (["paper"], ["note"], ["record"]):
        st, body, ms = call(base, "POST", "/api/search",
                            {"query": q, "top_k": 8, "mode": "hybrid", "fusion": "rrf",
                             "filters": {"kinds": kinds}})
        hits = body.get("hits", []) if isinstance(body, dict) else []
        actual = sorted({h.get("doc_kind") for h in hits})
        check(f"filter doc_kind={kinds[0]}", st == 200 and actual == kinds,
              f"n={len(hits)} kinds={actual} {ms:.0f}ms")
    st, body, ms = call(base, "POST", "/api/search",
                        {"query": q, "top_k": 8, "mode": "hybrid", "fusion": "rrf",
                         "filters": {"langs": ["en"]}})
    hits = body.get("hits", []) if isinstance(body, dict) else []
    check("filter lang=en", st == 200 and all(h.get("lang") == "en" for h in hits),
          f"n={len(hits)}")

    section("POST /api/search —— 参数校验")
    st, body, _ = call(base, "POST", "/api/search", {"query": ""})
    check("空查询被拒绝", st >= 400, f"status={st} detail={str(body)[:120]}")
    st, body, _ = call(base, "POST", "/api/search", {"query": q, "mode": "nonsense"})
    check("非法 mode 返回 400 而不是 500", st == 400, f"status={st} {str(body)[:120]}")

    section("POST /api/compare —— 三路对比")
    st, body, ms = call(base, "POST", "/api/compare", {"query": q, "top_k": 8})
    ok = st == 200 and isinstance(body, dict) and body.get("rows")
    check("compare 200 且有行", bool(ok), f"{ms:.0f} ms rows={len((body or {}).get('rows', []))}")
    if isinstance(body, dict):
        print("    agreement: " + json.dumps(body.get("agreement"), ensure_ascii=False))
        modes = body.get("modes") or {}
        for k, v in modes.items():
            print(f"    {k:<10} elapsed={v.get('elapsed_ms')}ms ids={v.get('ids')}")

    section("GET /api/chunks/{id} + /related")
    st, sres, _ = call(base, "POST", "/api/search", {"query": q, "top_k": 1, "mode": "semantic"})
    cid = (sres.get("hits") or [{}])[0].get("chunk_id") if isinstance(sres, dict) else None
    check("拿到一个 chunk_id", cid is not None, f"chunk_id={cid}")
    if cid is not None:
        st, body, ms = call(base, "GET", f"/api/chunks/{cid}")
        check(f"GET /api/chunks/{cid}", st == 200, f"{ms:.0f} ms")
        st, body, ms = call(base, "GET", f"/api/chunks/{cid}/related?k=5")
        rel = body.get("hits") if isinstance(body, dict) else body
        check(f"GET /api/chunks/{cid}/related", st == 200 and len(rel or []) > 0,
              f"n={len(rel or [])} {ms:.0f} ms")
        st, body, _ = call(base, "GET", "/api/chunks/99999999")
        check("不存在的 chunk 返回 404", st == 404, f"status={st}")

    section("GET /api/logs")
    st, body, ms = call(base, "GET", "/api/logs?limit=5")
    logs = body.get("logs") if isinstance(body, dict) else body
    check("logs 200", st == 200, f"{ms:.0f} ms n={len(logs or [])}")
    if logs:
        for lg in logs[:3]:
            print(f"    [{lg.get('created_at')}] {lg.get('mode')}/{lg.get('fusion')} "
                  f"n={lg.get('n_results')} {lg.get('elapsed_ms')}ms "
                  f"{(lg.get('query_text') or '')[:34]}")
        check("日志里的中文没被编码破坏",
              "\ufffd" not in json.dumps(logs, ensure_ascii=False))

    section("GET /api/ingest/preview")
    st, body, ms = call(base, "GET", "/api/ingest/preview")
    check("ingest preview 200", st == 200, f"{ms:.0f} ms")
    if isinstance(body, dict):
        print("    " + json.dumps({k: v for k, v in body.items() if k != "files"},
                                  ensure_ascii=False)[:300])
        print(f"    files: {len(body.get('files') or [])} 个")

    section("静态资源")
    for p, want in (("/", 200), ("/app.js", 200), ("/style.css", 200),
                    ("/favicon.svg", 200), ("/nope.js", 404)):
        st, body, ms = call(base, "GET", p)
        check(f"GET {p}", st == want, f"status={st} (期望 {want}) {ms:.0f} ms")
    # 目录穿越必须被挡住：403（识别出越界）或 404（当成不存在）都算通过，
    # 只要绝不把 app/ 下的源码吐出来。
    for p in ("/../app/config.py", "/..%2fapp%2fconfig.py", "/static/../../app/db.py"):
        st, body, ms = call(base, "GET", p)
        leaked = isinstance(body, bytes) and b"SEEKDB_PASSWORD" in body
        check(f"目录穿越被拦截 {p}", st in (400, 403, 404) and not leaked,
              f"status={st} {ms:.0f} ms")

    section("全部预设问题走一遍混合检索（HTTP 端到端）")
    total_wall = 0.0
    total_db = 0.0
    for item in PRESET_QUERIES:
        st, body, ms = call(base, "POST", "/api/search",
                            {"query": item["text"], "top_k": 8, "mode": "hybrid", "fusion": "rrf"})
        ok = st == 200 and isinstance(body, dict) and body.get("n_results", 0) > 0
        total_wall += ms
        total_db += float((body or {}).get("elapsed_ms") or 0)
        n = (body or {}).get("n_results")
        top = ((body or {}).get("hits") or [{}])[0]
        check(f"「{item['text'][:22]}」", ok,
              f"n={n} db={((body or {}).get('elapsed_ms'))}ms wall={ms:.0f}ms "
              f"top={top.get('doc_title', '')[:26]}")
    print(f"\n  平均：seekdb 侧 {total_db / len(PRESET_QUERIES):.1f} ms，"
          f"HTTP 端到端 {total_wall / len(PRESET_QUERIES):.1f} ms")

    section("结果")
    print(f"  PASS {PASS}   FAIL {FAIL}")
    if NOTES:
        print("\n  失败明细：")
        for n in NOTES:
            print(f"    - {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
