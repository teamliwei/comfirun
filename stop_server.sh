#!/usr/bin/env bash
# Stop a background-started ComfyRun server (see start_bg.sh).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f server.pid ]]; then
  echo "No server.pid found - nothing to stop."
  exit 0
fi

PID=$(cat server.pid)
if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping server (pid $PID)..."
  kill "$PID" 2>/dev/null || true
  sleep 2
  kill -9 "$PID" 2>/dev/null || true
fi
rm -f server.pid

# Orphaned tunnel from the killed process, if any.
pkill -f "cloudflared tunnel" 2>/dev/null || true
echo "Server stopped."
