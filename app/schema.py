"""seekdb 建库建表。

关键设计约束（均在 docs/SEEKDB_EVIDENCE.md 中有实测记录）：

1. `chunk` 表**不设用户主键**。seekdb 的原生混合检索接口
   `dbms_hybrid_search.search()` 依赖隐藏列 `__pk_increment` 做 FULL JOIN，
   若表带用户主键会直接报错：
       ERROR 1235: table with user provided primary key not supported
   因此 chunk_id / doc_id 只建普通二级索引。

2. 一张表同时挂三类能力：
       VECTOR   INDEX  —— 语义检索（cosine + HNSW + APPROXIMATE）
       FULLTEXT INDEX  —— 关键词检索（ik 中文分词 + BM25）
       普通列 + 索引   —— 结构化过滤（文档类型 / 语言 / 页码 / 时间）
   三路可以在**同一条 SQL** 里下推执行，这是本项目选择 seekdb 的核心原因。

3. `doc` / `search_log` 不参与原生混合检索，保留主键以获得唯一性与自增能力。

4. 全文索引要建**三个**，这是被 1191 逼出来的结论（实测见 SEEKDB_EVIDENCE.md）：

       ERROR 1191: Can't find FULLTEXT index matching the column list

   MySQL/seekdb 要求 `MATCH(col...)` 的列清单与某个 FULLTEXT 索引的列清单
   **完全一致**才算命中索引；而 `dbms_hybrid_search` 生成的全文分支永远是
   **单列** match：

       GREATEST(match(`content`) against('...' in natural language mode),
                match(`section_path`) against('...' in natural language mode))

   所以只有复合索引 ft_chunk(content, section_path) 时，原生 DSL 一律 1191，
   而手写的 `MATCH(content, section_path)` 反而正常。两条路都要保住，就得：

       ft_content (content)                     → 服务原生 DSL 的 content 分支
       ft_section (section_path)                → 服务原生 DSL 的 section_path 分支
       ft_chunk   (content, section_path)       → 服务手写复合 MATCH（BM25 打分更稳）

   同一张表挂多个 FULLTEXT 索引 seekdb 是允许的（probe 已验证），
   代价只是建索引时间，508 段语料下可以忽略。
"""
from __future__ import annotations

import logging

from . import config, db

log = logging.getLogger("seeklit.schema")


def database_ddl() -> str:
    return (
        f"CREATE DATABASE IF NOT EXISTS `{config.SEEKDB_DATABASE}` "
        f"DEFAULT CHARSET utf8mb4"
    )


def chunk_ddl(dim: int) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS `chunk` (
  `chunk_id`     BIGINT       NOT NULL,
  `doc_id`       VARCHAR(64)  NOT NULL,
  `doc_title`    VARCHAR(512),
  `doc_kind`     VARCHAR(32),
  `source_path`  VARCHAR(1024),
  `lang`         VARCHAR(8),
  `page_no`      INT,
  `section_path` VARCHAR(1024),
  `line_start`   INT,
  `line_end`     INT,
  `chunk_index`  INT,
  `tags`         VARCHAR(512),
  `content`      TEXT,
  `content_hash` CHAR(32),
  `emb`          VECTOR({int(dim)}),
  `created_at`   DATETIME,
  KEY `idx_chunk_id` (`chunk_id`),
  KEY `idx_doc_id` (`doc_id`),
  KEY `idx_doc_kind` (`doc_kind`),
  FULLTEXT INDEX `ft_content` (`content`) WITH PARSER ik,
  FULLTEXT INDEX `ft_section` (`section_path`) WITH PARSER ik,
  FULLTEXT INDEX `ft_chunk` (`content`, `section_path`) WITH PARSER ik,
  VECTOR INDEX `v_chunk_emb` (`emb`) WITH (DISTANCE=COSINE, TYPE=HNSW)
)
""".strip()


DOC_DDL = """
CREATE TABLE IF NOT EXISTS `doc` (
  `doc_id`      VARCHAR(64)  NOT NULL,
  `title`       VARCHAR(512),
  `source_path` VARCHAR(1024),
  `doc_kind`    VARCHAR(32),
  `lang`        VARCHAR(8),
  `n_chunks`    INT DEFAULT 0,
  `n_chars`     INT DEFAULT 0,
  `sha256`      CHAR(64),
  `tags`        VARCHAR(512),
  `imported_at` DATETIME,
  PRIMARY KEY (`doc_id`)
)
""".strip()


SEARCH_LOG_DDL = """
CREATE TABLE IF NOT EXISTS `search_log` (
  `log_id`       BIGINT NOT NULL AUTO_INCREMENT,
  `query_text`   VARCHAR(1024),
  `mode`         VARCHAR(16),
  `fusion`       VARCHAR(16),
  `weight`       DECIMAL(5,3),
  `n_results`    INT,
  `elapsed_ms`   DECIMAL(10,3),
  `sql_text`     TEXT,
  `top_doc_ids`  VARCHAR(1024),
  `created_at`   DATETIME,
  PRIMARY KEY (`log_id`),
  KEY `idx_created` (`created_at`)
)
""".strip()


SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS `demo_snapshot` (
  `snap_id`     BIGINT NOT NULL AUTO_INCREMENT,
  `label`       VARCHAR(256),
  `query_text`  VARCHAR(1024),
  `payload`     LONGTEXT,
  `created_at`  DATETIME,
  PRIMARY KEY (`snap_id`)
)
""".strip()


def ensure_schema(dim: int | None = None) -> dict:
    """建库建表；已存在则跳过。返回本次执行的 DDL 摘要。"""
    dim = int(dim or config.EMBED_DIM)
    executed: list[str] = []

    # 建库需要在未选定 database 的连接上执行
    import pymysql

    raw = pymysql.connect(
        host=config.SEEKDB_HOST, port=config.SEEKDB_PORT,
        user=config.SEEKDB_USER, password=config.SEEKDB_PASSWORD,
        charset="utf8mb4", autocommit=True, connect_timeout=15,
    )
    try:
        with raw.cursor() as cur:
            cur.execute(database_ddl())
            executed.append(f"database `{config.SEEKDB_DATABASE}`")
    finally:
        raw.close()

    for ddl, label in (
        (chunk_ddl(dim), f"chunk (VECTOR({dim}) HNSW + FULLTEXT ik ×3)"),
        (DOC_DDL, "doc"),
        (SEARCH_LOG_DDL, "search_log"),
        (SNAPSHOT_DDL, "demo_snapshot"),
    ):
        db.execute(ddl)
        executed.append(label)

    # CREATE TABLE IF NOT EXISTS 对老库是空操作，缺失的全文索引要在这里补上，
    # 否则 dbms_hybrid_search 会一直 1191。
    mig = ensure_fulltext_indexes("chunk")
    if mig["added"] or mig["rebuilt"]:
        executed.append(f"migrate fulltext: +{mig['added']} ~{mig['rebuilt']}")

    log.info("schema ready: %s", ", ".join(executed))
    return {"database": config.SEEKDB_DATABASE, "dim": dim,
            "created_or_existing": executed, "fulltext_migration": mig}


def has_table(name: str) -> bool:
    """判断表是否已建好。

    首次运行（还没执行过导入）时表并不存在，此时统计类接口应当返回空值
    而不是抛 1146，否则 CLI / 前端会直接报错退出。
    """
    try:
        rows = db.query("SHOW TABLES LIKE %s", (name,))
        return bool(rows)
    except Exception:  # noqa: BLE001
        return False


# 需要保证存在的全文索引：名字 -> (列元组, parser)
# 列清单必须与 MATCH(...) 的列清单逐一对应，顺序也要一致。
_REQUIRED_FULLTEXT: dict[str, tuple[tuple[str, ...], str]] = {
    "ft_content": (("content",), "ik"),
    "ft_section": (("section_path",), "ik"),
    "ft_chunk": (("content", "section_path"), "ik"),
}


def existing_fulltext_indexes(table: str = "chunk") -> dict[str, tuple[str, ...]]:
    """返回 {索引名: (列, ...)}，只含 FULLTEXT 类型。"""
    if not has_table(table):
        return {}
    out: dict[str, list] = {}
    for r in db.query(f"SHOW INDEX FROM `{table}`"):
        if str(r.get("Index_type", "")).upper() != "FULLTEXT":
            continue
        out.setdefault(str(r["Key_name"]), []).append(str(r["Column_name"]))
    return {k: tuple(v) for k, v in out.items()}


def ensure_fulltext_indexes(table: str = "chunk") -> dict:
    """给已存在的表补齐缺失的全文索引（在线迁移，不重灌数据）。

    `CREATE TABLE IF NOT EXISTS` 对已经建好的表是空操作，所以老库升级时
    必须显式 ALTER。缺哪个补哪个，已经一致的跳过，列清单不一致的才重建。
    """
    if not has_table(table):
        return {"table": table, "added": [], "rebuilt": [], "skipped": []}

    have = existing_fulltext_indexes(table)
    added: list[str] = []
    rebuilt: list[str] = []
    skipped: list[str] = []

    for name, (cols, parser) in _REQUIRED_FULLTEXT.items():
        col_sql = ", ".join(f"`{c}`" for c in cols)
        if name not in have:
            db.execute(
                f"ALTER TABLE `{table}` ADD FULLTEXT INDEX `{name}` ({col_sql}) "
                f"WITH PARSER {parser}"
            )
            added.append(name)
        elif have[name] != cols:
            db.execute(f"ALTER TABLE `{table}` DROP INDEX `{name}`")
            db.execute(
                f"ALTER TABLE `{table}` ADD FULLTEXT INDEX `{name}` ({col_sql}) "
                f"WITH PARSER {parser}"
            )
            rebuilt.append(name)
        else:
            skipped.append(name)

    if added or rebuilt:
        log.info("fulltext indexes updated on `%s`: added=%s rebuilt=%s",
                 table, added, rebuilt)
    return {"table": table, "added": added, "rebuilt": rebuilt, "skipped": skipped}


def table_info() -> list[dict]:
    """返回 chunk 表的真实 DDL，用于前端「运行环境」面板自证索引结构。"""
    if not has_table("chunk"):
        return []
    row = db.query_one("SHOW CREATE TABLE `chunk`")
    if not row:
        return []
    ddl = list(row.values())[-1]
    return [{"ddl": ddl}]
