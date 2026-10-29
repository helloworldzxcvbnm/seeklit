#!/usr/bin/env bash
# 启动一个【隔离的】seekdb 实例：独立 base-dir、独立端口，不碰机器上其他进程。
# 用法： bash scripts/start_seekdb.sh [port]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-${SEEKDB_PORT:-10100}}"
BIN="${SEEKDB_BIN:-$ROOT/bin/seekdb}"
BASE="$ROOT/runtime/seekdb"
PIDFILE="$ROOT/runtime/seekdb.pid"
LOG="$ROOT/logs/seekdb.log"

mkdir -p "$BASE" "$ROOT/logs"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "seekdb already running (pid $(cat "$PIDFILE"), port $PORT)"
  exit 0
fi

if [ ! -x "$BIN" ]; then
  echo "ERROR: seekdb binary not found at $BIN" >&2
  echo "       put the official seekdb binary there, or export SEEKDB_BIN=/path/to/seekdb" >&2
  exit 1
fi

nohup "$BIN" --nodaemon \
  --base-dir "$BASE" \
  --port "$PORT" \
  --parameter datafile_size=2G \
  --parameter datafile_maxsize=8G \
  --parameter log_disk_size=4G \
  >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"

# 等端口就绪（最多 60s）
for i in $(seq 1 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    exec 3>&- 3<&- || true
    echo "seekdb up on 127.0.0.1:$PORT (pid $(cat "$PIDFILE"))"
    exit 0
  fi
  sleep 1
done

echo "ERROR: seekdb did not open port $PORT within 60s; see $LOG" >&2
exit 1
