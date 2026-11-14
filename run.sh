#!/usr/bin/env bash
# ComfyRun server RUNNER for Colab / fresh Linux VMs.
#
# This script ONLY starts or stops the server. Environment preparation
# (dependencies, cloudflared, ComfyUI + models) is done by setup.sh - run
# that first, once. This script never downloads or installs anything.
#
# With a tunnel (the default) the server runs IN THE BACKGROUND: the script
# waits until the API answers /status AND the Cloudflare URL is out, prints
# the URL prominently, and RETURNS so the notebook cell completes. The URL
# block is never printed while the server is still initializing: the tunnel
# itself is started by the app only after it is ready to serve requests.
#
# Usage:
#   bash run.sh              # real ComfyUI bridge + tunnel (needs setup.sh + GPU)
#   bash run.sh --fg         # foreground, blocking run (with tunnel)
#   bash run.sh --no-tunnel  # foreground, local-only
#   bash run.sh stop         # stop a background instance
set -euo pipefail
cd "$(dirname "$0")"

LOG=server.log
PIDFILE=server.pid
WAIT_SECONDS="${WAIT_SECONDS:-180}"

MODE_STOP=0
FG=0
TUNNEL=1
for arg in "$@"; do
  case "$arg" in
    --no-tunnel) TUNNEL=0 ;;
    --fg) FG=1 ;;
    stop) MODE_STOP=1 ;;
    *) echo "Unknown flag: $arg"; exit 1 ;;
  esac
done

# ------------------------------------------------------------ stop action --
if [[ "$MODE_STOP" == "1" ]]; then
  if [[ -f "$PIDFILE" ]]; then
    PID=$(cat "$PIDFILE")
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      echo "Stopping background server (pid $PID)..."
      kill "$PID" 2>/dev/null || true
      sleep 2
      kill -9 "$PID" 2>/dev/null || true
    fi
  fi
  pkill -f "cloudflared tunnel" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "Server stopped."
  exit 0
fi

echo "== ComfyRun server run =="
echo "python: $(python3 --version)"

# ------------------------------------------------------- environment check --
# Readiness of the environment is setup.sh's job; this script only verifies
# it and points at setup.sh if it is missing.
if ! python3 -c "import fastapi, uvicorn, multipart" >/dev/null 2>&1; then
  echo "ERROR: server dependencies are not installed." >&2
  echo "  Run once:   bash setup.sh" >&2
  exit 1
fi

if [[ ! -f ComfyUI/main.py ]]; then
  echo "ERROR: ComfyUI is not installed here ($(pwd)/ComfyUI)." >&2
  echo "  The server needs the ComfyUI checkout + models." >&2
  echo "  Run once:   bash setup.sh     (long: clones ComfyUI + downloads models)" >&2
  exit 1
fi

if [[ "$TUNNEL" == "1" ]] && ! command -v cloudflared >/dev/null 2>&1; then
  echo "ERROR: cloudflared is not installed." >&2
  echo "  Run once:   bash setup.sh" >&2
  exit 1
fi

# ---------------------------------------------------------------- launch --
echo "== Starting ComfyRun server (REAL: ComfyUI subprocess on 127.0.0.1:8188) =="
ARGS=()
if [[ "$TUNNEL" == "1" ]]; then
  ARGS+=(--tunnel)
fi

if [[ "$FG" == "1" || "$TUNNEL" == "0" ]]; then
  # Blocking run (user asked for foreground, or local-only without a URL).
  exec python3 main.py "${ARGS[@]}"
fi

# Background run: detach, then wait for the URL + a /status answer and print it.
if [[ -f "$PIDFILE" ]]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null || true)
  if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
    echo "Stopping previous background server (pid $OLD)..."
    kill "$OLD" 2>/dev/null || true
    sleep 2
  fi
fi

echo "Starting server in background (log: $(pwd)/$LOG)..."
# Full detachment: new session (setsid), stdin cut off, output to the log
# file only. The process is disowned so NOTHING stays attached to the
# notebook cell - not ComfyUI, not the tunnel, nothing.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup python3 main.py "${ARGS[@]}" >> "$LOG" 2>&1 < /dev/null &
else
  nohup python3 main.py "${ARGS[@]}" >> "$LOG" 2>&1 < /dev/null &
fi
PID=$!
echo "$PID" > "$PIDFILE"
disown "$PID" 2>/dev/null || true
echo "pid: $PID"

DEADLINE=$((SECONDS + WAIT_SECONDS))
STATUS_URL="http://127.0.0.1:8000/status"
while (( SECONDS < DEADLINE )); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "SERVER PROCESS EXITED during startup. Reason (one line shown by: !tail -n 30 $LOG)"
    exit 1
  fi
  # The URL appears in the log only after the app is ready (the tunnel is
  # started by the app in its lifespan, post-initialization).
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" | head -1 || true)
  if [[ -n "$URL" ]]; then
    if ! command -v curl >/dev/null 2>&1; then
      echo ""
      echo "======================================================="
      echo "  SERVER IS UP (running in background)"
      echo "  URL:  $URL"
      echo "  logs: !tail -n 50 $LOG   |   stop: bash run.sh stop"
      echo "======================================================="
      exit 0
    fi
    if curl -sf -m 2 "$STATUS_URL" >/dev/null 2>&1; then
      echo ""
      echo "======================================================="
      echo "  SERVER IS UP (running in background)"
      echo "  URL:  $URL"
      echo "  logs: !tail -n 50 $LOG   |   stop: bash run.sh stop"
      echo "======================================================="
      exit 0
    fi
    echo "Tunnel URL found ($URL) - waiting for the API to answer /status ..."
  fi
  sleep 1
done

# Not an error: the server is still starting/running in the background.
echo "Server URL not confirmed yet after ${WAIT_SECONDS}s - the server continues starting in the background."
echo "Check it with: !tail -n 20 $LOG"
exit 0
