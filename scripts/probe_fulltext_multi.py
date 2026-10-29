"""探测 seekdb 对「同一张表上多个 FULLTEXT 索引」的支持程度。

背景（实测记录在 docs/SEEKDB_EVIDENCE.md）：
    dbms_hybrid_search 生成的 SQL 里，全文分支永远是**单列** match(`col`)，
    而 MySQL/seekdb 要求 MATCH 的列清单必须与某个 FULLTEXT 索引的列清单
    完全一致，否则报 1191。所以复合索引 ft_chunk(content, section_path)
    服务不了原生 DSL。

要同时保住两条路：
    原生 dbms_hybrid_search  → 需要 ft_content(content) [+ ft_section(section_path)]
    手写 MATCH(content, section_path) → 需要 ft_chunk(content, section_path)

本脚本在**临时探测表** probe_ft 上验证，不碰 chunk 表。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from app import db  # noqa: E402

TABLE = "probe_ft"


def drop() -> None:
    db.execute(f"DROP TABLE IF EXISTS `{TABLE}`")


def show(label: str, ddl: str) -> bool:
    """尝试建表，打印结果。返回是否成功。"""
    drop()
    print(f"\n--- {label} ---")
    print(ddl.strip())
    try:
        db.execute(ddl)
    except Exception as exc:  # noqa: BLE001
        print(f"CREATE FAILED: {type(exc).__name__}: {exc}")
        return False
    print("CREATE OK")
    rows = db.query(f"SHOW INDEX FROM `{TABLE}`")
    for r in rows:
        print(f"   {r.get('Key_name'):<14} seq={r.get('Seq_in_index')} "
              f"col={r.get('Column_name'):<14} type={r.get('Index_type')}")
    return True


DDL_THREE = f"""
CREATE TABLE `{TABLE}` (
  `chunk_id`     BIGINT NOT NULL,
  `section_path` VARCHAR(1024),
  `content`      TEXT,
  KEY `idx_chunk_id` (`chunk_id`),
  FULLTEXT INDEX `ft_content` (`content`) WITH PARSER ik,
  FULLTEXT INDEX `ft_section` (`section_path`) WITH PARSER ik,
  FULLTEXT INDEX `ft_chunk` (`content`, `section_path`) WITH PARSER ik
)
"""


def insert_samples() -> int:
    rows = [
        ("卫星姿态系统异常", "CUSUM 累积和检测用于姿态控制回路的微小漂移"),
        ("遥测规则汇总", "阈值规则与滑动窗口规则在遥测参数越限检测中的对比"),
        ("Anomaly Detection", "We propose a hybrid retrieval method combining BM25 and dense vectors"),
        ("实验记录", "本次实验在 256 维向量上运行，召回率与耗时见下表"),
    ]
    n = 0
    for i, (sec, txt) in enumerate(rows, start=1):
        db.execute(
            f"INSERT INTO `{TABLE}` (chunk_id, section_path, content) VALUES (%s,%s,%s)",
            (i, sec, txt),
        )
        n += 1
    return n


def try_sql(label: str, sql: str) -> None:
    print(f"\n  [{label}]")
    print(f"    {sql}")
    try:
        rows = db.query(sql, ("姿态",) * sql.count("%s"))
        print(f"    OK rows={len(rows)}")
        for r in rows:
            print(f"       {r}")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED {type(exc).__name__}: {exc}")


def try_native(label: str, payload: dict) -> None:
    print(f"\n  [native {label}]")
    print(f"    {json.dumps(payload, ensure_ascii=False)}")
    text = json.dumps(payload, ensure_ascii=False)
    try:
        gen = db.scalar(f"SELECT dbms_hybrid_search.get_sql(%s, %s) AS s", (TABLE, text), default="")
        print("    get_sql OK:")
        for line in str(gen).splitlines():
            print(f"      {line}")
    except Exception as exc:  # noqa: BCE001
        print(f"    get_sql FAILED {type(exc).__name__}: {exc}")
    try:
        hits = db.scalar(f"SELECT dbms_hybrid_search.search(%s, %s) AS h", (TABLE, text), default=None)
        parsed = json.loads(hits) if isinstance(hits, (str, bytes, bytearray)) else (hits or [])
        print(f"    search OK hits={len(parsed)}")
    except Exception as exc:  # noqa: BLE001
        print(f"    search FAILED {type(exc).__name__}: {exc}")


def main() -> None:
    print("seekdb:", db.server_version())

    if not show("三索引方案：ft_content + ft_section + ft_chunk", DDL_THREE):
        print("\n三索引方案不可用。")
        ddl_two = DDL_THREE.replace(
            "  FULLTEXT INDEX `ft_section` (`section_path`) WITH PARSER ik,\n", "")
        if not show("两索引方案：ft_content + ft_chunk", ddl_two):
            print("\n两索引方案也不可用，退回单索引 ft_chunk。")
            drop()
            return
        two = True
    else:
        two = False

    n = insert_samples()
    print(f"\n插入样本 {n} 行")

    try_sql("手写复合 MATCH(content, section_path)",
            f"SELECT chunk_id, MATCH(`content`,`section_path`) AGAINST(%s "
            f"IN NATURAL LANGUAGE MODE) AS bm25 FROM `{TABLE}` "
            f"WHERE MATCH(`content`,`section_path`) AGAINST(%s IN NATURAL LANGUAGE MODE)")
    try_sql("手写单列 MATCH(content)",
            f"SELECT chunk_id, MATCH(`content`) AGAINST(%s IN NATURAL LANGUAGE MODE) AS bm25 "
            f"FROM `{TABLE}` WHERE MATCH(`content`) AGAINST(%s IN NATURAL LANGUAGE MODE)")

    qnode_two = {"multi_match": {"query": "姿态", "fields": ["content", "section_path"]}}
    qnode_one = {"multi_match": {"query": "姿态", "fields": ["content"]}}
    try_native("multi_match 两字段 + rrf", {
        "query": qnode_two,
        "rank": {"rrf": {"rank_constant": 60, "rank_window_size": 10}},
        "size": 4,
        "_source": ["chunk_id", "content"],
    })
    try_native("multi_match 单字段 content", {
        "query": qnode_one, "size": 4, "_source": ["chunk_id", "content"],
    })
    if not two:
        try_native("match on section_path 单列", {
            "query": {"match": {"section_path": "姿态"}},
            "size": 4, "_source": ["chunk_id", "section_path"],
        })

    drop()
    print("\nprobe_ft 已清理。")


if __name__ == "__main__":
    main()
