"""诊断：dbms_hybrid_search 到底生成了什么 SQL、需要什么样的 FULLTEXT 索引。

用法：  ./.venv/bin/python scripts/diag_native_hybrid.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, embedder  # noqa: E402

QTEXT = "持续小幅慢漂移用哪种方法在线检测"


def show(title: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False)
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print("DSL:", body[:400])
    try:
        sql = db.scalar("SELECT dbms_hybrid_search.get_sql(%s,%s) AS s", ("chunk", body), default="")
        print("--- get_sql ---")
        print(sql)
    except Exception as exc:  # noqa: BLE001
        print(f"get_sql FAILED: {type(exc).__name__}: {exc}")
        return
    try:
        raw = db.scalar("SELECT dbms_hybrid_search.search(%s,%s) AS h", ("chunk", body), default=None)
        hits = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else (raw or [])
        print(f"--- search OK, {len(hits)} hits ---")
        for h in hits[:3]:
            print(f"  _score={h.get('_score')}  {str(h.get('doc_title'))[:40]}")
    except Exception as exc:  # noqa: BLE001

        print(f"search FAILED: {type(exc).__name__}: {exc}")


def main() -> int:
    print("seekdb:", db.server_version())
    print("FULLTEXT 索引现状：")
    for r in db.query("SHOW INDEX FROM `chunk`"):
        if r.get("Index_type") in ("FULLTEXT", "VECTOR") or "ft" in str(r.get("Key_name", "")) \
           or "v_" in str(r.get("Key_name", "")):
            print("  ", {k: r[k] for k in ("Key_name", "Column_name", "Index_type", "Seq_in_index") if k in r})

    emb = embedder.get_embedder()
    if not emb.fitted:
        emb.load()
    vec = [round(float(x), 8) for x in emb.encode_one(QTEXT)]
    print("向量维度:", len(vec))

    knn = {"field": "emb", "query_vector": vec, "k": 10}
    src = ["chunk_id", "doc_title", "content"]

    # 1) multi_match 两字段
    show("A. multi_match fields=[content, section_path]", {
        "query": {"multi_match": {"query": QTEXT, "fields": ["content", "section_path"]}},
        "knn": knn, "size": 5, "_source": src,
    })

    # 2) multi_match 单字段
    show("B. multi_match fields=[content]", {
        "query": {"multi_match": {"query": QTEXT, "fields": ["content"]}},
        "knn": knn, "size": 5, "_source": src,
    })

    # 3) match 单字段
    show("C. match on content", {
        "query": {"match": {"content": QTEXT}},
        "knn": knn, "size": 5, "_source": src,
    })

    # 4) 只有 knn，没有 query（纯向量）
    show("D. knn only", {"knn": knn, "size": 5, "_source": src})

    # 5) 只有 query，没有 knn（纯全文）
    show("E. query only", {
        "query": {"match": {"content": QTEXT}}, "size": 5, "_source": src,
    })

    # 6) 加 rank.rrf
    show("F. match + knn + rank.rrf", {
        "query": {"match": {"content": QTEXT}},
        "knn": knn,
        "rank": {"rrf": {"rank_constant": 60, "rank_window_size": 20}},
        "size": 5, "_source": src,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
