-- ============================================================================
--  溯文 SeekLit —— seekdb 建表脚本
--  目标库：OceanBase seekdb v1.4.0.0（MySQL 协议）
--
--  本文件与 app/schema.py 中的 DDL 完全一致，单独放一份是为了让评审
--  不读 Python 也能直接看到「一张表挂三类索引」的结构。
--  服务器上的真实表结构见 docs/SEEKDB_EVIDENCE.md 第 1 节（SHOW CREATE TABLE 原文）。
--
--  两个反直觉但被实测逼出来的设计：
--
--  1) chunk 表【不设用户主键】。
--     seekdb 的原生混合检索 dbms_hybrid_search.search() 依赖隐藏列
--     __pk_increment 做 FULL JOIN；表一旦带用户主键就直接报
--         ERROR 1235: table with user provided primary key not supported
--     所以 chunk_id / doc_id 只建普通二级索引。
--
--  2) 全文索引要建【三个】而不是一个复合索引。
--     MySQL/seekdb 要求 MATCH(col...) 的列清单与某个 FULLTEXT 索引的列清单
--     完全一致；而 dbms_hybrid_search 生成的全文分支永远是单列 match：
--         GREATEST(match(`content`) against(...), match(`section_path`) against(...))
--     只有复合索引时原生 DSL 一律报
--         ERROR 1191: Can't find FULLTEXT index matching the column list
--     于是：ft_content 服务原生 content 分支，ft_section 服务原生 section_path
--     分支，ft_chunk 服务手写的复合 MATCH（两列一起打 BM25 更稳）。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `seeklit` DEFAULT CHARSET utf8mb4;
USE `seeklit`;

-- ----------------------------------------------------------------------------
-- chunk：检索的唯一主表。VECTOR + FULLTEXT×3 + 标量过滤列，同表共存。
-- ----------------------------------------------------------------------------
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
  `emb`          VECTOR(256),
  `created_at`   DATETIME,
  KEY `idx_chunk_id` (`chunk_id`),
  KEY `idx_doc_id` (`doc_id`),
  KEY `idx_doc_kind` (`doc_kind`),
  FULLTEXT INDEX `ft_content` (`content`) WITH PARSER ik,
  FULLTEXT INDEX `ft_section` (`section_path`) WITH PARSER ik,
  FULLTEXT INDEX `ft_chunk` (`content`, `section_path`) WITH PARSER ik,
  VECTOR INDEX `v_chunk_emb` (`emb`) WITH (DISTANCE=COSINE, TYPE=HNSW)
);

-- ----------------------------------------------------------------------------
-- doc：文档级元信息（导入去重、文档库面板）
-- ----------------------------------------------------------------------------
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
);

-- ----------------------------------------------------------------------------
-- search_log：每次检索留痕（query / mode / fusion / 耗时 / 真实 SQL）
-- ----------------------------------------------------------------------------
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
);

-- ----------------------------------------------------------------------------
-- demo_snapshot：导出给 Qoder Sites 的离线快照存档
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `demo_snapshot` (
  `snap_id`     BIGINT NOT NULL AUTO_INCREMENT,
  `label`       VARCHAR(256),
  `query_text`  VARCHAR(1024),
  `payload`     LONGTEXT,
  `created_at`  DATETIME,
  PRIMARY KEY (`snap_id`)
);

-- ----------------------------------------------------------------------------
-- 已存在旧表（只有复合全文索引）时的在线补齐，不重灌数据：
--   ALTER TABLE `chunk` ADD FULLTEXT INDEX `ft_content` (`content`) WITH PARSER ik;
--   ALTER TABLE `chunk` ADD FULLTEXT INDEX `ft_section` (`section_path`) WITH PARSER ik;
-- 等价命令：python -m app.cli migrate
-- ----------------------------------------------------------------------------
