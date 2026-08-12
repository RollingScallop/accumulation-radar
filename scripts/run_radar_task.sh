#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-full}"
PAUSE_FILE="${RADAR_PAUSE_FILE:-$BASE_DIR/.radar_paused}"
PYTHON_BIN="${PYTHON_BIN:-$BASE_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

case "$MODE" in
  pool|oi|observe|manage|seed-observe|full) ;;
  *)
    echo "Usage: $0 {pool|oi|observe|manage|seed-observe|full}" >&2
    exit 2
    ;;
esac

# 暂停哨兵：文件存在且首行不是 RESUMED 时暂停。
# (远程会话无法删除文件，改为写入 RESUMED 标记来恢复运行)
if [[ -f "$PAUSE_FILE" ]] && ! head -n1 "$PAUSE_FILE" | grep -qi '^RESUMED'; then
  echo "[$(date '+%F %T')] skipped $MODE: radar tasks paused by $PAUSE_FILE"
  exit 0
fi

LOCK_DIR="/tmp/accumulation-radar-${MODE}.lock"
LOCK_MAX_AGE_SEC="${LOCK_MAX_AGE_SEC:-1200}"
STAMP_FILE="$BASE_DIR/.last_${MODE}_ts"
NOW="$(date +%s)"

if [[ -d "$LOCK_DIR" ]]; then
  if [[ -f "$LOCK_DIR/pid" ]]; then
    PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      STARTED="$(cat "$LOCK_DIR/started" 2>/dev/null || echo "$NOW")"
      AGE="$((NOW - STARTED))"
      if (( AGE < LOCK_MAX_AGE_SEC )); then
        echo "[$(date '+%F %T')] skipped $MODE: lock busy"
        exit 0
      fi
    fi
  fi
  rm -rf "$LOCK_DIR"
fi

mkdir "$LOCK_DIR"
trap 'rm -rf "$LOCK_DIR"' EXIT
echo "$$" > "$LOCK_DIR/pid"
echo "$NOW" > "$LOCK_DIR/started"

cd "$BASE_DIR"
export RADAR_ENV_FILE="${RADAR_ENV_FILE:-$BASE_DIR/.env.oi}"

# 运行时覆盖（远程会话无法改 .env.oi；python 侧用 os.environ.setdefault，环境变量优先）
export ENABLE_TG_PUSH="${ENABLE_TG_PUSH:-true}"
export ENABLE_OBSERVE_TG_PUSH="${ENABLE_OBSERVE_TG_PUSH:-false}"
export AI_TRADER_AUTO_EXECUTE="${AI_TRADER_AUTO_EXECUTE:-true}"

case "$MODE" in
  manage)
    "$PYTHON_BIN" "$BASE_DIR/scripts/local_status_monitor.py" --sync-once
    ;;
  *)
    "$PYTHON_BIN" "$BASE_DIR/accumulation_radar.py" "$MODE"
    ;;
esac

date +%s > "$STAMP_FILE"
