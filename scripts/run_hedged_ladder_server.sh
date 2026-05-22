#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${HEDGED_LADDER_HOST:-127.0.0.1}"
PORT="${HEDGED_LADDER_PORT:-8787}"

echo "Starting hedged ladder backend on http://${HOST}:${PORT}"
echo "Live trading allowed: ${HEDGED_LADDER_ALLOW_LIVE:-false}"

exec python3 scripts/hedged_ladder_server.py --host "$HOST" --port "$PORT"
