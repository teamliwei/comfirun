#!/usr/bin/env bash
# Start the ComfyRun server IN THE BACKGROUND (Colab / notebook friendly).
#
# Usage:
#   bash start_bg.sh              # mock mode + tunnel (fast pipeline test)
#   bash start_bg.sh --setup      # one-time ComfyUI + model setup (long)
#   bash start_bg.sh --real       # real ComfyUI generation (needs --setup done)
#   bash start_bg.sh --no-tunnel  # bind locally only
#
# Launches the server detached (nohup), writes all output to server.log,
# then waits for the Cloudflare tunnel URL and prints it prominently before
# returning, so the notebook cell completes.
set -euo pipefail
cd "$(dirname "$0")"

LOG="${LOG:-server.log}"
PIDFILE="server.pid"
WAIT_SECONDS="${WAIT_SECONDS:-180}"

# Stop a previously started background instance.
if [[ -f "$PIDFILE" ]]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null || true)
  if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
    echo "Stopping previous server (pid $OLD)..."
    kill "$OLD" 2>/dev/null || true
    sleep 2
  fi
fi

echo "Starting server in background (log: $(pwd)/$LOG)..."
nohup bash run.sh "$@" > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "pid: $PID"

# Wait for the tunnel URL, or for the process to die.
DEADLINE=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < DEADLINE )); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo ""
    echo "=== server process exited - last log lines: ==="
    tail -n 30 "$LOG"
    exit 1
  fi
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" | head -1 || true)
  if [[ -n "$URL" ]]; then
    echo ""
    echo "======================================================="
    echo "  SERVER IS UP"
    echo "  URL:  $URL"
    echo "  saved to: data/tunnel_url.txt"
    echo "  view logs:  !tail -n 50 $LOG"
    echo "  stop:       bash stop_server.sh"
    echo "======================================================="
    exit 0
  fi
  sleep 1
done

echo "Tunnel URL not seen after ${WAIT_SECONDS}s - last log lines:"
tail -n 30 "$LOG"
exit 1
