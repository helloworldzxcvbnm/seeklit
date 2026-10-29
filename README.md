# 溯文 SeekLit —— 基于 OceanBase seekdb 的科研文献与笔记混合检索

> Qoder × OceanBase 参赛作品。方向：**文献与资料检索** ——
> 把论文、课程笔记、实验记录导入 seekdb，按语义和关键词混合检索，快速找到相关段落和出处。
> 数据库仅使用 **seekdb**（v1.4.0.0），不借助任何其他向量/全文引擎。

在线体验与项目介绍站点见 Qoder Sites 发布页（链接见仓库顶部 / 站点「体验入口」），
站点内嵌了一份**真实查询结果快照**：不连服务器也能切换 semantic / keyword / hybrid、
拖语义权重滑块与 RRF rank_constant，每个分数都用快照里记录的真实 vsim / bm25 在浏览器里重算。

## 解决的问题

科研资料的检索痛点是「措辞对不上」和「术语不能错」同时存在：

- 问「持续小幅慢漂移用哪种方法在线检测」，正文里的术语是 **CUSUM / EWMA**，字面检索无从命中；
- 问「CUSUM 的 k 和 h 怎么标定」，`k`、`h`、`ARL` 是精确词元，纯向量检索容易漏；
- 问「AGC 上升 RSL 下降属于哪一类异常」，缩写必须靠全文索引精确命中。

单一检索路必然漏召回。本项目实测：8 个真实问题里，**平均 3.0 段只有语义能找到、3.0 段只有关键词能命中**，
混合检索把两部分都保住（详见 `docs/SEEKDB_EVIDENCE.md` 第 5 节的效果表）。

## 核心功能

1. **四路检索**：semantic（VECTOR + cosine + HNSW）、keyword（FULLTEXT + ik 分词 + BM25）、
   hybrid 原生 RRF（`dbms_hybrid_search`，融合发生在数据库内部）、hybrid 可调权重（单条 SQL 内归一化 + 加权）。
2. **结构化过滤下推**：文档类型 / 语言 / 页码与向量、全文检索在同一条 SQL 的执行计划里下推。
3. **三路对比面板**：同一问题在三条路径下的排名差异、仅语义 / 仅关键词段落一目了然。
4. **以段找段**：复用向量索引做段落到段落近邻，顺着一条判据找到同族方法。
5. **SQL 透明**：每次检索返回 seekdb 实际执行/生成的 SQL（含原生混合检索的内部生成 SQL），前端可展开逐字核对。
6. **检索留痕**：`search_log` 记录每次查询的模式、权重、耗时与 SQL。
7. **离线快照导出**：把真实结果 + 原始分数导出为 JSON，供静态站点内嵌交互演示。
8. **多格式导入**：pdf / md / txt / csv / tsv，按大纲、页、行块、滑窗分块，自动识别中英文与文档类型。

## 架构

```
corpus_raw/ (pdf md txt csv)
      │  app/chunking.py   分块 + 标题合成 + 语言/标签
      ▼
app/ingest.py ── app/embedder.py (LSA / ollama / openai / sbert)
      ▼
seekdb 单表 chunk：VECTOR(emb) + FULLTEXT×3(ik) + 标量过滤列
      ▼
app/search.py  四路径 + 过滤下推 + 三路对比 + 以段找段
      ▼
app/server.py  标准库 http.server（零 Web 依赖）── app/static/  vanilla JS 工作台
      ▼
app/snapshot.py  真实结果快照 ── Qoder Sites 离线交互演示
```

## 快速开始

```bash
# 1. 放语料：corpus_raw/{notes,records,papers}/ 下放 md txt pdf csv
# 2. 一键：隔离 seekdb 实例 → 依赖 → 建表导入 → 效果验证 → 快照 → 起服务
bash scripts/setup.sh
# 3. 打开 http://127.0.0.1:8090/
```

单独步骤：

```bash
bash scripts/start_seekdb.sh          # 隔离实例（独立 base-dir + 端口 10100）
pip install -r requirements.txt       # 只有 pymysql + pypdf 是必装
python -m app.cli ingest --mode rebuild
python -m app.cli info                # 版本 / 语料规模 / 表结构 / 全文索引清单
python -m app.cli search "CUSUM 的 k 和 h 怎么标定" --mode hybrid --fusion rrf --show-sql
python -m app.cli compare "单点毛刺应该用哪个方法"
python -m app.cli eval                # 8 个预设问题 × 3 路径的效果表
python -m app.cli snapshot            # 导出站点快照 JSON
python -m app.cli serve --port 8090
bash scripts/stop_seekdb.sh
```

配置全部走环境变量或 `.env`（样例见 `.env.example`）；建表 DDL 见 `sql/schema.sql`。

## HTTP API（14 个端点）

`GET /api/health` `/api/stats` `/api/docs` `/api/preset-queries` `/api/documents`
`/api/documents/{doc_id}` `/api/chunks/{chunk_id}` `/api/chunks/{chunk_id}/related` `/api/logs`；
`POST /api/search` `/api/compare` `/api/ingest/preview`。
`/api/search` 返回命中段落、分数分解（vec_score / bm25 / fused / 两路排名）、真实 SQL 与耗时。

## 目录结构

```
app/            后端 + 前端（server / search / schema / ingest / chunking / embedder / snapshot / cli + static/）
sql/schema.sql  建表脚本（含两个反直觉设计的注释）
scripts/        setup / start_seekdb / stop_seekdb / verify_paths / probe / profile / smoke_api / cleanup
docs/           SEEKDB_EVIDENCE.md（实测证据集）· QODER_DEVLOG.md（开发记录）· shots/（演示截图）
logs/           verify_paths.txt · eval_postfix.txt · smoke_api.txt（原始采集文本）
snapshot/       导出的站点快照 JSON
```

## 验证与效果（摘要）

- HTTP 冒烟 **46 PASS / 0 FAIL**（`logs/smoke_api.txt`），含 4 条检索路径、过滤下推、参数校验、目录穿越拦截。
- 8 个预设问题 × 3 路径全部 8/8 召回；平均两路共有 5.0、仅语义 3.0、仅关键词 3.0、混合排名变动 4.5。
- seekdb 侧平均耗时：semantic 4.12 ms / keyword 3.70 ms / 原生 RRF 116.38 ms / 加权单 SQL 9.90 ms。
- 完整证据（SHOW CREATE TABLE 原文、四路径真实 SQL、原生 RRF 生成 SQL 解剖、1191 根因）见 `docs/SEEKDB_EVIDENCE.md`。

## 比赛要求对照

| 要求 | 落点 |
| --- | --- |
| 导入论文/笔记/实验记录 | `app/ingest.py` + `app/chunking.py`，26 篇 / 508 段实测 |
| 语义 + 关键词混合检索 | 四路径 + 三路对比，融合在 seekdb 内完成 |
| 快速找到相关段落和出处 | 命中段带 doc_title / section_path / 行号 / 页码，可「以段找段」 |
| 可运行性 | `bash scripts/setup.sh` 一键复现；46 项冒烟全过 |
| 技术结合 | VECTOR + FULLTEXT×3 + 标量过滤同表同计划；原生 dbms_hybrid_search |
| 工程质量 | 零 Web 依赖、配置外置、在线迁移、留痕、证据文档、开发记录 |
