#!/bin/zsh

BASE_DIR="/Users/sabrina0x/accumulation-radar"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"
SCRIPT="$BASE_DIR/scripts/local_status_monitor.py"
PID_FILE="$BASE_DIR/.local_monitor_loop.pid"
LOG_FILE="$BASE_DIR/monitor_loop.log"

export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"
export ALL_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"; exit 0' INT TERM EXIT

while true; do
  "$PYTHON_BIN" "$SCRIPT" >> "$LOG_FILE" 2>&1
  sleep 30
done
