#!/bin/zsh

set -u

LABEL="ai.accumulation.radar.monitor"
PLIST="/Users/sabrina0x/Library/LaunchAgents/${LABEL}.plist"
PYTHON_BIN="/Users/sabrina0x/accumulation-radar/.venv/bin/python"
SCRIPT="/Users/sabrina0x/accumulation-radar/scripts/local_status_monitor.py"
RETRY_SCRIPT="/Users/sabrina0x/accumulation-radar/scripts/manual_retry_observer_trade.py"
LOOP_SCRIPT="/Users/sabrina0x/accumulation-radar/scripts/run_local_monitor_forever.sh"
LOOP_PID_FILE="/Users/sabrina0x/accumulation-radar/.local_monitor_loop.pid"

cmd="${1:-status}"

status_monitor() {
  launchctl print "gui/$(id -u)/${LABEL}"
}

is_loaded() {
  launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1
}

start_monitor() {
  if is_loaded; then
    launchctl kickstart -k "gui/$(id -u)/${LABEL}"
    return
  fi

  launchctl bootstrap "gui/$(id -u)" "$PLIST" || {
    if is_loaded; then
      launchctl kickstart -k "gui/$(id -u)/${LABEL}"
      return
    fi
    return 1
  }
}

stop_monitor() {
  launchctl bootout "gui/$(id -u)/${LABEL}"
}

run_once() {
  "$PYTHON_BIN" "$SCRIPT"
}

loop_status() {
  if [ -f "$LOOP_PID_FILE" ]; then
    local pid
    pid="$(cat "$LOOP_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      echo "loop running pid=$pid"
      return 0
    fi
  fi
  echo "loop not running"
  return 1
}

start_loop() {
  if loop_status >/dev/null 2>&1; then
    loop_status
    return 0
  fi
  nohup "$LOOP_SCRIPT" >/dev/null 2>&1 &
  sleep 1
  loop_status
}

stop_loop() {
  if [ ! -f "$LOOP_PID_FILE" ]; then
    echo "loop not running"
    return 0
  fi
  local pid
  pid="$(cat "$LOOP_PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$LOOP_PID_FILE"
  echo "loop stopped"
}

retry_trade() {
  local symbol="${2:-}"
  if [ -z "$symbol" ]; then
    echo "Usage: $0 retry <SYMBOL>"
    return 1
  fi
  "$PYTHON_BIN" "$RETRY_SCRIPT" "$symbol"
}

case "$cmd" in
  start)
    start_monitor
    ;;
  stop)
    stop_monitor
    ;;
  restart)
    stop_monitor >/dev/null 2>&1 || true
    start_monitor
    ;;
  status)
    status_monitor
    ;;
  run-once)
    run_once
    ;;
  loop-start)
    start_loop
    ;;
  loop-stop)
    stop_loop
    ;;
  loop-status)
    loop_status
    ;;
  retry)
    retry_trade "$@"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|run-once|loop-start|loop-stop|loop-status|retry <SYMBOL>}"
    exit 1
    ;;
esac
