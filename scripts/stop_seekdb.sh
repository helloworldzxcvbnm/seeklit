#!/usr/bin/env bash
# 停止由 start_seekdb.sh 启动的隔离 seekdb 实例。
# 只杀 pidfile 里的进程；pidfile 丢失时按 base-dir 精确匹配，绝不误伤其他实例。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$ROOT/runtime/seekdb"
PIDFILE="$ROOT/runtime/seekdb.pid"

pid=""
if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE")"
  kill -0 "$pid" 2>/dev/null || pid=""
fi
if [ -z "$pid" ]; then
  pid="$(pgrep -f -- "--base-dir $BASE" | head -1 || true)"
fi

if [ -z "$pid" ]; then
  echo "seekdb (base-dir $BASE) is not running"
  rm -f "$PIDFILE"
  exit 0
fi

echo "stopping seekdb pid $pid ..."
kill "$pid"
for i in $(seq 1 30); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
  echo "still alive after 30s, sending SIGTERM again"
  kill "$pid" || true
fi
rm -f "$PIDFILE"
echo "seekdb stopped"
