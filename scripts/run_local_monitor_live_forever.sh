#!/bin/zsh

BASE_DIR="/Users/sabrina0x/accumulation-radar"
ENV_FILE="$BASE_DIR/.env.live"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"
SCRIPT="$BASE_DIR/scripts/local_status_monitor.py"
PID_FILE="$BASE_DIR/.local_monitor_live_loop.pid"
LOG_FILE="$BASE_DIR/monitor_live_loop.log"
RUN_TASK_SCRIPT="$BASE_DIR/scripts/run_radar_task_live.sh"

if [ ! -f "$ENV_FILE" ]; then
  echo "missing $ENV_FILE; copy .env.live.example and fill live config first"
  exit 2
fi

set -a
source "$ENV_FILE"
set +a

export RADAR_ENV_FILE="$ENV_FILE"
export MONITOR_STATE_PATH="$BASE_DIR/.monitor_live_state.json"
export MONITOR_LOG_PATH="$BASE_DIR/monitor_live_status.log"
export RUN_RADAR_TASK_SCRIPT="$BASE_DIR/scripts/run_radar_task_live.sh"

export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"
export ALL_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"; exit 0' INT TERM EXIT

while true; do
  "$PYTHON_BIN" "$SCRIPT" >> "$LOG_FILE" 2>&1
  now="$(date +%s)"

  observe_interval="${LIVE_OBSERVE_INTERVAL_SEC:-60}"
  last_observe="$(cat "$BASE_DIR/.live_last_observe_ts" 2>/dev/null || echo 0)"
  if [ $((now - last_observe)) -ge "$observe_interval" ]; then
    echo "$now" > "$BASE_DIR/.live_last_observe_ts"
    "$RUN_TASK_SCRIPT" observe >> "$LOG_FILE" 2>&1
  fi

  oi_interval="${LIVE_OI_INTERVAL_SEC:-900}"
  last_oi="$(cat "$BASE_DIR/.live_last_oi_ts" 2>/dev/null || echo 0)"
  if [ $((now - last_oi)) -ge "$oi_interval" ]; then
    echo "$now" > "$BASE_DIR/.live_last_oi_ts"
    "$RUN_TASK_SCRIPT" oi >> "$LOG_FILE" 2>&1
  fi

  pool_interval="${LIVE_POOL_INTERVAL_SEC:-14400}"
  last_pool="$(cat "$BASE_DIR/.live_last_pool_ts" 2>/dev/null || echo 0)"
  if [ $((now - last_pool)) -ge "$pool_interval" ]; then
    echo "$now" > "$BASE_DIR/.live_last_pool_ts"
    "$RUN_TASK_SCRIPT" pool >> "$LOG_FILE" 2>&1
  fi

  sleep 30
done
