#!/bin/zsh

BASE_DIR="/Users/sabrina0x/accumulation-radar"
ENV_FILE="$BASE_DIR/.env.live"

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

"$BASE_DIR/.venv/bin/python" "$BASE_DIR/scripts/local_status_monitor.py" >> "$BASE_DIR/monitor_live_status.log" 2>&1
