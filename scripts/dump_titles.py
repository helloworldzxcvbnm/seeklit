"""Dump distinct doc titles + chunk counts, so titles can be sanity-checked.

ASCII-only source on purpose: this runs over plink, and non-ASCII in a
remote -c string gets re-encoded by the local code page.
"""
import sys

sys.path.insert(0, ".")

from app import db  # noqa: E402

rows = db.query(
    "SELECT d.doc_id, d.title, d.doc_kind, d.lang, d.n_chunks, d.n_chars "
    "FROM `doc` d ORDER BY d.doc_kind, d.doc_id"
)
print(f"{'doc_id':<46} {'kind':<8} {'lang':<5} {'n':>4} {'chars':>8}  title")
print("-" * 124)
for r in rows:
    print(f"{str(r['doc_id']):<46} {str(r['doc_kind']):<8} {str(r['lang']):<5} "
          f"{str(r['n_chunks']):>4} {str(r['n_chars'] or 0):>8}  {r['title']}")
print(f"\ntotal docs: {len(rows)}")
