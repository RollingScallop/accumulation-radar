#!/bin/zsh

set -u

MODE="${1:-observe}"
BASE_DIR="/Users/sabrina0x/accumulation-radar"
ENV_FILE="$BASE_DIR/.env.live"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"
SCRIPT_PATH="$BASE_DIR/accumulation_radar.py"
LOG_FILE="$BASE_DIR/accumulation_live_${MODE}.log"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S')"

if [ ! -f "$ENV_FILE" ]; then
  echo "[$RUN_TS] missing $ENV_FILE; copy .env.live.example and fill live config first" >> "$LOG_FILE"
  exit 2
fi

case "$MODE" in
  manage)
    LOCK_FILE="/tmp/accumulation-radar-live-manage.lock"
    ;;
  observe)
    LOCK_FILE="/tmp/accumulation-radar-live-observe.lock"
    ;;
  *)
    LOCK_FILE="/tmp/accumulation-radar-live-core.lock"
    ;;
esac

export RADAR_ENV_FILE="$ENV_FILE"
export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"
export ALL_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

mkdir -p "$BASE_DIR"

lockf -s -t 0 "$LOCK_FILE" "$PYTHON_BIN" "$SCRIPT_PATH" "$MODE" >> "$LOG_FILE" 2>&1
STATUS=$?

if [ "$STATUS" -eq 75 ]; then
  echo "[$RUN_TS] skipped live ${MODE}: lock busy" >> "$LOG_FILE"
fi

exit 0
