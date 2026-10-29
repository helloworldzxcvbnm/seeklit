"""Where does the per-query wall time actually go?

Section 6 of verify_paths.txt showed ~90-120 ms wall per query while seekdb
reported only ~4 ms. This splits the remainder into: embedder access, query
tokenize+encode, connection acquire, and the SQL round trip.
"""
import sys
import time

sys.path.insert(0, ".")

from app import db, embedder, search  # noqa: E402

Q = "CUSUM"


def t(label, fn, n=5):
    best = 1e9
    total = 0.0
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        d = (time.perf_counter() - t0) * 1000
        total += d
        best = min(best, d)
    print(f"  {label:<34} avg={total / n:8.2f} ms  best={best:8.2f} ms")


print("seekdb:", db.server_version())
emb = embedder.get_embedder()
print("embedder:", emb.describe())

t("get_embedder()", lambda: embedder.get_embedder())
t("embedder.tokenize(Q)", lambda: embedder.tokenize(Q))
t("emb.encode_one(Q)", lambda: emb.encode_one(Q))

vec = emb.encode_one(Q)
t("db.POOL.connection() acquire+ping", lambda: db.query("SELECT 1"))
t("cosine_distance over 508 rows",
  lambda: db.query(
      f"SELECT chunk_id, cosine_distance(`emb`, '{db.vector_literal(vec)}') AS d "
      f"FROM `chunk` ORDER BY d APPROXIMATE LIMIT 8"))

print()
t("search(semantic, top_k=8)", lambda: search.search(Q, mode="semantic", top_k=8, log_query=False))
t("search(keyword, top_k=8)", lambda: search.search(Q, mode="keyword", top_k=8, log_query=False))
t("search(hybrid/weighted, top_k=8)",
  lambda: search.search(Q, mode="hybrid", fusion="weighted", top_k=8, log_query=False))
t("search(hybrid/rrf, top_k=8)",
  lambda: search.search(Q, mode="hybrid", fusion="rrf", top_k=8, log_query=False))

print()
# rank_window_size drives the fts LIMIT (20x). Measure the cost of a big window.
for rws in (8, 20, 60):
    t(f"hybrid/rrf rank_window_size={rws}",
      lambda rws=rws: search.search(Q, mode="hybrid", fusion="rrf", top_k=8,
                                    candidate_k=rws, log_query=False), n=3)
