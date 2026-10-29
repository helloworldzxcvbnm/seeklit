"""seekdb 连接层。

seekdb 100% 兼容 MySQL 协议，这里直接用 PyMySQL 建一个轻量连接池，
所有检索都以「原始 SQL」的形式下发，方便把真实 SQL 展示给使用者与评审。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import pymysql
from pymysql.cursors import DictCursor

from . import config

log = logging.getLogger("seeklit.db")


class SeekDBError(RuntimeError):
    pass


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.SEEKDB_HOST,
        port=config.SEEKDB_PORT,
        user=config.SEEKDB_USER,
        password=config.SEEKDB_PASSWORD,
        database=config.SEEKDB_DATABASE,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=15,
        read_timeout=300,
        write_timeout=300,
        local_infile=False,
    )


class ConnectionPool:
    """线程安全的最小连接池。

    seekdb 的会话级变量（ob_query_timeout）按连接设置，因此连接归还时不重置，
    复用的连接会保留首次建立时设置的超时。
    """

    def __init__(self, size: int = 8) -> None:
        self._pool: queue.Queue = queue.Queue(maxsize=size)
        self._size = size
        self._created = 0
        self._lock = threading.Lock()

    def _new_conn(self) -> pymysql.connections.Connection:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SET ob_query_timeout = %s", (config.SEEKDB_QUERY_TIMEOUT,))
            cur.execute("SET ob_trx_timeout = %s", (config.SEEKDB_QUERY_TIMEOUT,))
        return conn

    def _acquire(self) -> pymysql.connections.Connection:
        """拿一个可用连接：优先复用池里的，池空且未达上限就新建，否则阻塞等待。"""
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            create = False
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    create = True
            if not create:
                try:
                    conn = self._pool.get(timeout=60)
                except queue.Empty as exc:
                    raise TimeoutError(
                        f"等待空闲 seekdb 连接超时（60s，池上限 {self._size}）") from exc
            else:
                try:
                    conn = self._new_conn()
                except Exception:
                    # 建连失败要把配额还回去，否则池会永久缩水
                    with self._lock:
                        self._created -= 1
                    raise
        # 探活：长时间空闲后 seekdb 可能已经单方面断开
        try:
            conn.ping(reconnect=True)
        except Exception:  # noqa: BLE001
            self._discard(conn)
            conn = self._new_conn()
        return conn

    def _discard(self, conn: pymysql.connections.Connection | None) -> None:
        with self._lock:
            self._created = max(0, self._created - 1)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @contextmanager
    def connection(self):
        conn = self._acquire()
        broken = False
        try:
            yield conn
        except Exception:
            broken = True
            raise
        finally:
            if broken:
                self._discard(conn)
            else:
                try:
                    self._pool.put_nowait(conn)
                except queue.Full:  # pragma: no cover
                    self._discard(conn)

    def close_all(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._created = 0


POOL = ConnectionPool(size=8)


@contextmanager
def cursor():
    with POOL.connection() as conn:
        with conn.cursor() as cur:
            yield cur


def query(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """执行查询，返回 dict 列表。"""
    t0 = time.perf_counter()
    with cursor() as cur:
        cur.execute(sql, params or ())
        rows = cur.fetchall()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.debug("query %.1fms rows=%d sql=%s", elapsed_ms, len(rows), sql[:200])
    return list(rows)


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: Sequence[Any] | None = None, default: Any = None) -> Any:
    row = query_one(sql, params)
    if not row:
        return default
    return next(iter(row.values()), default)


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


def execute_many(sql: str, seq_of_params: Iterable[Sequence[Any]]) -> int:
    """批量写入。seekdb 兼容 MySQL 的 executemany 语义。"""
    seq = list(seq_of_params)
    if not seq:
        return 0
    total = 0
    with cursor() as cur:
        for i in range(0, len(seq), config.DB_INSERT_BATCH):
            batch = seq[i : i + config.DB_INSERT_BATCH]
            cur.executemany(sql, batch)
            total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(batch)
    return total


def execute_script(script: str) -> None:
    """执行多语句脚本（用于建库建表）。"""
    statements = [s.strip() for s in script.split(";\n") if s.strip()]
    with cursor() as cur:
        for stmt in statements:
            if not stmt or stmt.startswith("--"):
                continue
            cur.execute(stmt)


def server_version() -> str:
    try:
        return str(scalar("SELECT VERSION() AS v", default="unknown"))
    except Exception as exc:  # noqa: BLE001
        # 必须带上异常类型：queue.Empty 这类异常 str() 是空的，
        # 只留 message 会让「连不上」变得完全无法定位（踩过一次）。
        return f"unreachable: {type(exc).__name__}: {exc}".rstrip(": ")


def vector_literal(values: Sequence[float]) -> str:
    """seekdb 的向量字面量格式：'[0.1,0.2,...]'。

    用 repr 级别的精度避免截断导致 cosine 距离抖动；限制 8 位小数已足够。
    """
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"
