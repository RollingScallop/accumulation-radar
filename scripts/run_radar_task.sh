#!/bin/zsh

set -u

MODE="${1:-observe}"
BASE_DIR="/Users/sabrina0x/accumulation-radar"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"
SCRIPT_PATH="$BASE_DIR/accumulation_radar.py"
LOG_FILE="$BASE_DIR/accumulation_${MODE}.log"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S')"

case "$MODE" in
  manage)
    LOCK_FILE="/tmp/accumulation-radar-db.lock"
    STAMP_FILE="/tmp/accumulation-radar-manage-last.ts"
    MIN_INTERVAL="${LOCAL_MANAGE_MIN_INTERVAL_SEC:-45}"
    ;;
  observe)
    LOCK_FILE="/tmp/accumulation-radar-db.lock"
    ;;
  *)
    LOCK_FILE="/tmp/accumulation-radar-db.lock"
    ;;
esac

export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"
export ALL_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

mkdir -p "$BASE_DIR"

if [ "$MODE" = "manage" ]; then
  NOW_TS="$(date '+%s')"
  if [ -f "$STAMP_FILE" ]; then
    LAST_TS="$(cat "$STAMP_FILE" 2>/dev/null || echo 0)"
    if [ $((NOW_TS - LAST_TS)) -lt "$MIN_INTERVAL" ]; then
      echo "[$RUN_TS] skipped ${MODE}: throttle ${NOW_TS}-${LAST_TS}<${MIN_INTERVAL}" >> "$LOG_FILE"
      exit 0
    fi
  fi
  echo "$NOW_TS" > "$STAMP_FILE"
fi

lockf -s -t 0 "$LOCK_FILE" "$PYTHON_BIN" "$SCRIPT_PATH" "$MODE" >> "$LOG_FILE" 2>&1
STATUS=$?

if [ "$STATUS" -eq 75 ]; then
  echo "[$RUN_TS] skipped ${MODE}: lock busy" >> "$LOG_FILE"
fi

exit 0
