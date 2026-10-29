#!/usr/bin/env bash
# 一键复现：隔离 seekdb 实例 → 依赖 → 建表导入 → 效果验证 → 快照 → 起服务。
# 全程只在本项目目录内操作（runtime/ data/ logs/ snapshot/），不碰系统其他进程。
# 用法： bash scripts/setup.sh [--skip-ingest]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SKIP_INGEST="${1:-}"

echo "== [1/6] seekdb 实例 =="
bash scripts/start_seekdb.sh

echo "== [2/6] Python 环境 =="
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
# jieba / numpy 在 requirements 里是注释掉的“可选”，这里显式装上以获得最佳中文分词
.venv/bin/pip install -q jieba numpy || echo "warn: jieba/numpy install failed, embedder will fall back"

echo "== [3/6] 语料 =="
if [ ! -d corpus_raw ] || [ -z "$(ls -A corpus_raw 2>/dev/null)" ]; then
  echo "corpus_raw/ 为空：把你的 pdf / md / txt / csv 按 notes/ records/ papers/ 三个子目录放进去再重跑本脚本"
  echo "（目录名决定 doc_kind：note / record / paper）"
fi
.venv/bin/python -m app.cli preview

echo "== [4/6] 建表 + 导入 =="
if [ "$SKIP_INGEST" != "--skip-ingest" ]; then
  .venv/bin/python -m app.cli ingest --mode rebuild
fi
.venv/bin/python -m app.cli info

echo "== [5/6] 效果验证（8 个预设问题 × 4 条路径）=="
.venv/bin/python -m app.cli eval --top-k 8

echo "== [6/6] 导出站点快照 + 启动服务 =="
.venv/bin/python -m app.cli snapshot
nohup .venv/bin/python -m app.cli serve --host 127.0.0.1 --port 8090 >> logs/serve.log 2>&1 &
echo $! > runtime/serve.pid
sleep 2
curl -s http://127.0.0.1:8090/api/health | head -c 300; echo
echo
echo "done. 打开 http://127.0.0.1:8090/ 使用检索工作台"
