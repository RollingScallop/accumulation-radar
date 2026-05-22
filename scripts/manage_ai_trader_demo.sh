#!/bin/zsh

set -u

LABEL="ai.trader.demo.backend"
PLIST="/Users/sabrina0x/Library/LaunchAgents/${LABEL}.plist"

cmd="${1:-status}"

is_loaded() {
  launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1
}

start_service() {
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

stop_service() {
  launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
}

case "$cmd" in
  start)
    start_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  stop)
    stop_service
    ;;
  status)
    launchctl print "gui/$(id -u)/${LABEL}"
    ;;
  *)
    echo "Usage: $0 {start|restart|stop|status}"
    exit 1
    ;;
esac
