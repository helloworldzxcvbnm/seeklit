"""One-shot cleanup of scratch tables and the mojibake search_log row.

`probe_doc` / `probe_np` were left behind by the early capability probes, and
one search_log row was written from a query that got re-encoded by the local
code page on its way through plink. Both would show up in the demo UI.

ASCII-only on purpose (runs over plink).
"""
import sys

sys.path.insert(0, ".")

from app import db  # noqa: E402

for t in ("probe_doc", "probe_np", "probe_ft"):
    try:
        db.execute(f"DROP TABLE IF EXISTS `{t}`")
        print(f"dropped {t}")
    except Exception as exc:  # noqa: BLE001
        print(f"drop {t} failed: {type(exc).__name__}: {exc}")

before = db.scalar("SELECT COUNT(*) AS c FROM `search_log`", default=0)
# fix_argv_encoding() 会把孤立代理项换成 U+FFFD，所以「乱码行」是合法 UTF-8，
# 只能按替换字符识别。这类行来自命令行编码事故，留在日志里会污染演示。
rows = db.query("SELECT log_id, query_text FROM `search_log`")
bad = [r["log_id"] for r in rows if "\ufffd" in (r.get("query_text") or "")]

for lid in bad:
    db.execute("DELETE FROM `search_log` WHERE log_id=%s", (lid,))

after = db.scalar("SELECT COUNT(*) AS c FROM `search_log`", default=0)
print(f"search_log: {before} -> {after} (removed {len(bad)} mangled rows)")

print("\nremaining tables:")
for r in db.query("SHOW TABLES"):
    print("  ", next(iter(r.values())))
