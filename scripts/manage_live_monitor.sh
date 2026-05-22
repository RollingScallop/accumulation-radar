#!/bin/zsh

set -u

BASE_DIR="/Users/sabrina0x/accumulation-radar"
ENV_FILE="$BASE_DIR/.env.live"
LOOP_SCRIPT="$BASE_DIR/scripts/run_local_monitor_live_forever.sh"
LOOP_PID_FILE="$BASE_DIR/.local_monitor_live_loop.pid"
RUN_TASK_SCRIPT="$BASE_DIR/scripts/run_radar_task_live.sh"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"
RETRY_SCRIPT="$BASE_DIR/scripts/manual_retry_observer_trade.py"

cmd="${1:-status}"

loop_status() {
  if [ -f "$LOOP_PID_FILE" ]; then
    local pid
    pid="$(cat "$LOOP_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      echo "live loop running pid=$pid"
      return 0
    fi
  fi
  echo "live loop not running"
  return 1
}

loop_start() {
  if loop_status >/dev/null 2>&1; then
    loop_status
    return 0
  fi
  nohup "$LOOP_SCRIPT" >/dev/null 2>&1 &
  sleep 1
  loop_status
}

loop_stop() {
  if [ ! -f "$LOOP_PID_FILE" ]; then
    echo "live loop not running"
    return 0
  fi
  local pid
  pid="$(cat "$LOOP_PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$LOOP_PID_FILE"
  echo "live loop stopped"
}

run_once() {
  "$BASE_DIR/scripts/run_local_monitor_live_once.sh"
}

run_mode() {
  local mode="${2:-}"
  if [ -z "$mode" ]; then
    echo "Usage: $0 run <oi|observe|manage|seed-observe|pool>"
    return 1
  fi
  "$RUN_TASK_SCRIPT" "$mode"
}

retry_trade() {
  local symbol="${2:-}"
  if [ -z "$symbol" ]; then
    echo "Usage: $0 retry <SYMBOL>"
    return 1
  fi
  RADAR_ENV_FILE="$ENV_FILE" "$PYTHON_BIN" "$RETRY_SCRIPT" "$symbol"
}

case "$cmd" in
  loop-start)
    loop_start
    ;;
  loop-stop)
    loop_stop
    ;;
  loop-status|status)
    loop_status
    ;;
  run-once)
    run_once
    ;;
  run)
    run_mode "$@"
    ;;
  retry)
    retry_trade "$@"
    ;;
  *)
    echo "Usage: $0 {loop-start|loop-stop|loop-status|run-once|run <mode>|retry <SYMBOL>}"
    exit 1
    ;;
esac
