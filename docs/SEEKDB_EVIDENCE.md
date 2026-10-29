# seekdb 实测证据集（溯文 SeekLit）

> 本文件里的**每一个分数、每一段 SQL、每一条耗时**都来自服务器上真实运行的
> OceanBase seekdb 查询，不是模拟数据。原始采集文本见
> `logs/verify_paths.txt`（建表/四路径/对比/以段找段）、`logs/eval_postfix.txt`（效果汇总）、
> `logs/smoke_api.txt`（HTTP 46 项冒烟）。
> 采集时间 2026-10-30，语料 26 篇文档 / 508 段 / 271,656 字符。

## 0. 运行环境

| 项 | 值 |
| --- | --- |
| seekdb 版本 | `5.7.25-OceanBase seekdb-v1.4.0.0` |
| 连接 | `127.0.0.1:10100/seeklit`，账号 `root@sys`（MySQL 协议，pymysql） |
| 实例隔离 | 独立 base-dir `/root/ryh/newproject/runtime/seekdb`，独立端口 10100，不影响同机其他进程 |
| 语料 | 26 篇 / 508 段 / 271,656 字符；note 19 篇 161 段，record 5 篇 59 段，paper 2 篇 288 段；中文 166 段 / 英文 342 段 |
| 向量化 | 默认离线 LSA：jieba 分词 → sublinear TF-IDF → 随机化截断 SVD，256 维，词表 6,417，纯 numpy |
| Web 层 | Python 标准库 `http.server`，零 Web 依赖（部署机出网仅十几 KB/s，装不动 FastAPI 依赖树，属有意选择） |

## 1. 一张表挂三类索引（SHOW CREATE TABLE 原文）

```sql
CREATE TABLE `chunk` (
  `chunk_id` bigint(20) NOT NULL,
  `doc_id` varchar(64) NOT NULL,
  `doc_title` varchar(512) DEFAULT NULL,
  `doc_kind` varchar(32) DEFAULT NULL,
  `source_path` varchar(1024) DEFAULT NULL,
  `lang` varchar(8) DEFAULT NULL,
  `page_no` int(11) DEFAULT NULL,
  `section_path` varchar(1024) DEFAULT NULL,
  `line_start` int(11) DEFAULT NULL,
  `line_end` int(11) DEFAULT NULL,
  `chunk_index` int(11) DEFAULT NULL,
  `tags` varchar(512) DEFAULT NULL,
  `content` text DEFAULT NULL,
  `content_hash` char(32) DEFAULT NULL,
  `emb` VECTOR(256) DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  KEY `idx_chunk_id` (`chunk_id`) BLOCK_SIZE 16384 LOCAL,
  KEY `idx_doc_id` (`doc_id`) BLOCK_SIZE 16384 LOCAL,
  KEY `idx_doc_kind` (`doc_kind`) BLOCK_SIZE 16384 LOCAL,
  FULLTEXT KEY `ft_chunk` (`content`, `section_path`) WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart") ...,
  VECTOR KEY `v_chunk_emb` (`emb`) WITH (DISTANCE=COSINE, TYPE=HNSW, LIB=VSAG, M=16,
         EF_CONSTRUCTION=200, EF_SEARCH=64) ...,
  FULLTEXT KEY `ft_content` (`content`) WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart") ...,
  FULLTEXT KEY `ft_section` (`section_path`) WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart") ...
) ORGANIZATION INDEX DEFAULT CHARSET = utf8mb4 ...
```

语义（VECTOR + cosine + HNSW/VSAG）、关键词（FULLTEXT ×3 + ik 中文分词 + BM25）、
结构化过滤（`doc_kind` / `lang` / 页码的标量列与 BTREE 索引）在同一张表上共存，
因此**三路可以在同一条 SQL 的执行计划里一起下推**——这是本项目选 seekdb 的核心理由。

两个反直觉设计（都被报错逼出来，见第 3 节与 3.5 节）：

1. `chunk` **不设用户主键**。`dbms_hybrid_search.search()` 依赖隐藏列 `__pk_increment`
   做 FULL JOIN，带用户主键直接 `ERROR 1235: table with user provided primary key not supported`。
2. 全文索引建**三个**：`ft_content(content)`、`ft_section(section_path)`、`ft_chunk(content, section_path)`。

## 2. 四条检索路径与真实 SQL

### 2.1 semantic —— VECTOR 索引

```sql
SELECT chunk_id, doc_id, ..., content,
       cosine_distance(`emb`, '[0.04847667,0.40445581,... 共 256 维]') AS vec_dist
FROM `chunk` WHERE 1=1
ORDER BY vec_dist APPROXIMATE LIMIT 8
```

`ORDER BY ... APPROXIMATE` 是让优化器走 HNSW 近似 ANN 的关键写法。
实测 seekdb 侧平均 **4.12 ms**（8 个预设问题），修复 embedder 单例 bug 后
含向量化的端到端平均 **21.8 ms**（见第 6 节）。返回示例（问题「持续小幅慢漂移用哪种方法在线检测」）：

```
#1 [note] 一、任务界定 | 二、逐方法汇总： > [M04 Page–Hinkl   vec=0.421329
#2 [note] 一、任务界定 | 三、各方法伪代码 > 3.4 M04 Page–Hi    vec=0.409971
#3 [record] method_id / method_name_cn | 数据表 行 22-24       vec=0.379771
```

注意该问题正文里的术语是 CUSUM / EWMA，**并不包含「在线检测」这个词组**——纯字面匹配无从命中，靠的是向量跨措辞匹配。

### 2.2 keyword —— FULLTEXT 索引 + BM25

```sql
SELECT chunk_id, ..., MATCH(`content`,`section_path`) AGAINST(%s IN NATURAL LANGUAGE MODE) AS bm25
FROM `chunk`
WHERE MATCH(`content`,`section_path`) AGAINST(%s IN NATURAL LANGUAGE MODE)
ORDER BY bm25 DESC LIMIT 8
```

ik 分词器（`ik_mode="smart"`）负责中文切词，`MATCH ... AGAINST ... IN NATURAL LANGUAGE MODE`
直接返回 BM25 分数。同一问题的关键词路结果与语义路**完全不同**：

```
#1 [note] 一、任务界定 | 二、逐方法汇总： > [M08 在线多尺度 MOSU  bm25=3.874648
#2 [note] SPOT 基本原理 |                                       bm25=3.174471
```

### 2.3 hybrid / 原生 RRF —— dbms_hybrid_search，融合发生在数据库内部

应用侧只提交 DSL：

```json
{
  "query": {"multi_match": {"query": "持续小幅慢漂移用哪种方法在线检测",
                            "fields": ["content", "section_path"]}},
  "knn":  {"field": "emb", "query_vector": "<256 维>", "k": 60},
  "rank": {"rrf": {"rank_constant": 60, "rank_window_size": 60}},
  "size": 8
}
```

用 `dbms_hybrid_search.get_sql()` 可以把 seekdb **自己生成的 SQL** 取出来（前端「查看 SQL」展示的就是它）。解剖后的关键结构：

```sql
SELECT ifnull(`_fts`.`chunk_id`, `_vs`.`chunk_id`) as `chunk_id`, ...,
       (ifnull(1 / (`_fts`.`_keyword_rank` + 60), 0)
      + ifnull(1 / (`_vs`.`_semantic_rank` + 60), 0)) as `_score`
FROM (
  SELECT *, RANK() over( ORDER BY `_keyword_score` DESC) as `_keyword_rank`
  FROM (SELECT /*+ opt_param('hidden_column_visible','true') */ ...,
               GREATEST(match(`content`) against('...' in natural language mode),
                        match(`section_path`) against('...' in natural language mode)) as `_keyword_score`
        FROM `seeklit`.`chunk`
        WHERE match(`content`) against('...' ...) OR match(`section_path`) against('...' ...)
        ORDER BY `_keyword_score` DESC LIMIT 1200) `_fts`
  FULL JOIN (
    SELECT *, RANK() over( ORDER BY `_semantic_score` DESC) as `_semantic_rank`
    FROM (SELECT /*+ opt_param('hidden_column_visible','true') */
                 cosine_distance(`emb`, '[...]') as `_distance`, ...,
                 round(1 - cosine_distance(`emb`, '[...]') / 2, 8) as `_semantic_score`
          FROM `seeklit`.`chunk` ORDER BY `_distance` APPROXIMATE LIMIT 60) ) `_vs`
  ON `_fts`.`__pk_increment` = `_vs`.`__pk_increment`)
ORDER BY `_score` DESC, ... LIMIT 8
```

从生成 SQL 里读出的四条硬事实（都可在前端 SQL 抽屉里逐字核对）：

1. **RRF 公式**：`_score = 1/(keyword_rank + 60) + 1/(semantic_rank + 60)`，60 即 DSL 里的 `rank_constant`，改它立刻改变融合结果。
2. **全文分支窗口**：`LIMIT 1200 = 20 × rank_window_size(60)`，是固定 20 倍关系，不是等于窗口值。
3. **语义分支打分**：`_semantic_score = round(1 - cosine_distance/2, 8)`，即把 cosine 距离折回 [0,1] 的相似度；非 RRF 模式下 `_score = keyword_score + semantic_score` 直接相加。
4. **两路用隐藏列 `__pk_increment` 做 FULL JOIN**——这正是 `chunk` 表不能有用户主键的原因（ERROR 1235）。

实测该路径 seekdb 侧平均 **116–134 ms**（窗口函数 + 双路召回的代价），8 个预设问题全部返回 8/8。

### 2.4 hybrid / 可调权重 —— 单条 SQL 完成两路召回 + 归一化 + 加权

```sql
WITH `v`  AS (SELECT chunk_id, (1 - cosine_distance(`emb`, '[...]')) AS vsim
              FROM `chunk` ORDER BY cosine_distance(`emb`, '[...]') APPROXIMATE LIMIT 60),
     `f`  AS (SELECT chunk_id, MATCH(`content`,`section_path`) AGAINST(%s IN NATURAL LANGUAGE MODE) AS bm25
              FROM `chunk` WHERE MATCH(`content`,`section_path`) AGAINST(%s IN NATURAL LANGUAGE MODE)
              ORDER BY bm25 DESC LIMIT 60),
     `vn` AS (SELECT chunk_id, vsim, MAX(vsim) OVER () AS vmax,
                     ROW_NUMBER() OVER (ORDER BY vsim DESC) AS vrank FROM `v`),
     `fn` AS (SELECT chunk_id, bm25, MAX(bm25) OVER () AS fmax,
                     ROW_NUMBER() OVER (ORDER BY bm25 DESC) AS frank FROM `f`)
SELECT c.*, vn.vsim AS vec_raw, vn.vrank AS vec_rank, fn.bm25 AS bm25_raw, fn.frank AS bm25_rank,
       ROUND(COALESCE(vn.vsim / NULLIF(vn.vmax,0), 0), 6) AS vec_score,
       ROUND(COALESCE(fn.bm25 / NULLIF(fn.fmax,0), 0), 6) AS bm25,
       ROUND(0.6 * COALESCE(vn.vsim / NULLIF(vn.vmax,0), 0)
           + 0.4 * COALESCE(fn.bm25 / NULLIF(fn.fmax,0), 0), 6) AS fused
FROM `vn` FULL JOIN `fn` ON vn.chunk_id = fn.chunk_id
JOIN `chunk` c ON c.chunk_id = COALESCE(vn.chunk_id, fn.chunk_id)
ORDER BY fused DESC, vec_rank ASC LIMIT 8
```

权重 `0.6/0.4` 是**写进 SQL 的字面量**，不是应用侧二次排序；窗口函数做 max 归一化，
FULL JOIN 保证「只被一路召回」的段落也参与融合。实测 seekdb 侧平均 **9.90 ms**。
前端权重滑块每改一档就重发这条 SQL，返回的 `vec_score / bm25 / fused / 两路排名` 直接渲染成分数条。

### 2.5 结构化过滤与向量、全文同计划下推

过滤条件进 DSL 的 `filter` 子句（`terms` on `doc_kind` / `lang`），seekdb 把它与
ANN、全文检索放进同一个执行计划，而不是召回后再过滤：

```json
{"query": {"bool": {"must": [{"multi_match": {"query": "...", "fields": ["content","section_path"]}}],
                    "filter": [{"terms": {"lang": ["en"]}}]}},
 "knn": {"field": "emb", "query_vector": [...], "k": 60}}
```

实测（top_k=8）：无过滤 n=8；`doc_kind=['paper']` n=8 / 110.961 ms；`doc_kind=['note']` n=8 / 93.138 ms；
`doc_kind=['record']` n=8 / 50.723 ms；`lang=['en']` n=8。手写单路 SQL 的过滤则直接拼 `WHERE doc_kind IN (...)`，与索引条件共存。

## 3. ERROR 1191：全文索引列清单必须与 MATCH 完全一致

现象：只有复合索引 `ft_chunk(content, section_path)` 时，**原生 DSL 的每次混合检索都报**

```
ERROR 1191: Can't find FULLTEXT index matching the column list
```

而手写的 `MATCH(content, section_path) AGAINST (...)` 却正常。根因在 2.3 节的生成 SQL 里看得很清楚：
`dbms_hybrid_search` 的全文分支永远生成**单列** `match(\`content\`)` 与 `match(\`section_path\`)`，
而 MySQL/seekdb 判定「MATCH 是否命中全文索引」的规则是**列清单与某个 FULLTEXT 索引完全相等**，
单列清单匹配不上双列索引，于是 1191。

验证与修复：

1. `scripts/probe_fulltext_multi.py` 在临时表上证明同一张表可以挂多个 FULLTEXT 索引，且单列/复合 MATCH 各自命中对应索引；
2. `sql/schema.sql` 改为三索引；对已存在的旧表提供在线迁移 `python -m app.cli migrate`
   （`ALTER TABLE ... ADD FULLTEXT INDEX`，不重灌数据），`cmd_info` 会打印全文索引清单并对缺失项告警；
3. 迁移后 6 种 DSL 变体（multi_match 单/双字段 × 有无 filter × RRF/加权）全部通过。

## 4. 以段找段：复用向量索引做段落到段落近邻

`/api/chunks/{id}/related` 不引入新模型，直接拿该段的 `emb` 当查询向量：

```sql
SELECT ..., cosine_distance(`emb`, '[该段向量]') AS vec_dist
FROM `chunk` WHERE chunk_id <> %s ORDER BY vec_dist APPROXIMATE LIMIT 5
```

实测 chunk_id=151 的 5 个近邻里，第 1 名是同一方法族的参数表行（vec=0.509738），
第 4、5 名是 M05 Hampel / M03 CUSUM 的伪代码段——与人工阅读预期的「同族方法」一致。

## 5. 效果数据（不做人工标注，报可观测指标）

8 个预设问题 × 三条路径，全部来自 `logs/eval_postfix.txt`（修复后采集）：

| 问题 | 语义 | 关键词 | 混合 | 两路共有 | 仅语义 | 仅关键词 | 排名变动 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 持续小幅慢漂移用哪种方法在线检测 | 8 | 8 | 8 | 5 | 3 | 3 | 5 |
| CUSUM 的 k 和 h 怎么标定 | 8 | 8 | 8 | 3 | 5 | 5 | 5 |
| 单点毛刺应该用哪个方法 | 8 | 8 | 8 | 6 | 2 | 2 | 4 |
| 报警拖尾导致误判故障仍在持续 | 8 | 8 | 8 | 5 | 3 | 3 | 4 |
| AGC 上升 RSL 下降属于哪一类异常 | 8 | 8 | 8 | 4 | 4 | 4 | 3 |
| 哪些方法对持续电平型故障结构性失明 | 8 | 8 | 8 | 7 | 1 | 1 | 6 |
| 误报预算怎么用蒙特卡洛重新标定 | 8 | 8 | 8 | 5 | 3 | 3 | 4 |
| unsupervised anomaly detection … | 8 | 8 | 8 | 5 | 3 | 3 | 5 |

平均：两路共有 5.00，仅语义 3.00，仅关键词 3.00，混合后排名变动 4.50。
**「仅语义」与「仅关键词」都稳定大于 0**，即任何单路都会漏掉另一路能找到的段落，
融合的价值在这张表上是可测量的，而不是口号。

耗时（seekdb 侧，8 问题平均）：semantic 4.12 ms / keyword 3.70 ms / 原生 RRF 116.38 ms / 加权单 SQL 9.90 ms。
修复 embedder 单例 bug 后的端到端（含向量化）：semantic 21.8 ms / keyword 20.6 ms / 原生 RRF 134.3 ms。
HTTP 冒烟（`logs/smoke_api.txt`，46 项全过）里 8 个预设问题走完整 HTTP 链路：seekdb 侧平均 196.2 ms、端到端平均 267.1 ms（含 SSH 隧道往返）。

## 6. 一个真实性能 bug 的复盘：embedder 单例失效

`app/embedder.py` 早期版本写的是 `global _EMBEDER`（少一个 D），于是模块级 `_EMBEDDER`
永远是 `None`，**每次检索都重新读一遍 6.5 MB 的 LSA 模型 npz**，平白多约 49 ms。
`scripts/profile_query.py` 把耗时拆成「向量化 / SQL / 融合」三段后定位到它。
修复（正确的 global + 双重检查锁）前后对比：

| 路径 | 修复前 | 修复后 |
| --- | --- | --- |
| semantic | 91.1 ms | 9.9 ms |
| keyword | 79.5 ms | 3.7 ms |
| hybrid/weighted | 105.4 ms | 21.0 ms |
| hybrid/rrf | 130.5 ms | 66.0 ms |

## 7. 每次检索都留痕

`search_log` 表记录 query / mode / fusion / weight / n_results / elapsed_ms / 真实 SQL / top doc_ids，
前端「运行环境与 SQL 证据」面板可直接翻最近记录。导出快照时关闭留痕（`log_query=False`），
避免演示数据污染统计。

## 8. 复现命令

```bash
bash scripts/setup.sh                 # 隔离实例 → 依赖 → 导入 → eval → 快照 → 起服务
.venv/bin/python -m app.cli eval      # 重跑第 5 节的效果表
.venv/bin/python scripts/verify_paths.py   # 重采第 1–4 节的 SQL 证据
.venv/bin/python scripts/smoke_api.py --base http://127.0.0.1:8090   # 46 项 HTTP 冒烟
```
