#!/usr/bin/env bash
# ComfyRun server bootstrap for Colab / fresh Linux VMs.
#
# Installs dependencies, optionally prepares ComfyUI + models, then starts
# the server. With a tunnel (the default) the server runs IN THE BACKGROUND:
# the script waits for the Cloudflare URL, prints it prominently, and
# RETURNS so the notebook cell completes. All server output goes to
# server.log. `bash run.sh stop` shuts a background instance down.
#
# Usage:
#   bash run.sh                    # FAST: deps + mock server + tunnel (background)
#   bash run.sh --setup            # one-time: clone ComfyUI, custom nodes, download models
#   bash run.sh --real             # real ComfyUI bridge (requires --setup done once + GPU)
#   bash run.sh --fg               # foreground, blocking run (with tunnel)
#   bash run.sh --no-tunnel        # foreground, local-only
#   bash run.sh stop               # stop a background instance
set -euo pipefail
cd "$(dirname "$0")"

LOG=server.log
PIDFILE=server.pid
WAIT_SECONDS="${WAIT_SECONDS:-180}"

MODE_STOP=0
FG=0
DO_SETUP=0
MOCK=1
TUNNEL=1
for arg in "$@"; do
  case "$arg" in
    --setup) DO_SETUP=1 ;;
    --real) MOCK=0 ;;
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

echo "== ComfyRun server bootstrap =="
echo "python: $(python3 --version)"

# ------------------------------------------------- real mode preconditions --
if [[ "$MOCK" == "0" && "$DO_SETUP" == "0" ]]; then
  if [[ ! -f ComfyUI/main.py ]]; then
    echo "ERROR: ComfyUI is not installed here ($(pwd)/ComfyUI)." >&2
    echo "  Real mode needs the ComfyUI checkout + models." >&2
    echo "  Run once:   bash run.sh --setup     (long: clones ComfyUI + downloads models)" >&2
    echo "  Then start: bash run.sh --real" >&2
    exit 1
  fi
fi

# --------------------------------------------------------------- dependencies --
echo "== [1/3] Installing server dependencies =="
python3 -m pip install -q -r requirements.txt

# ------------------------------------------------- optional ComfyUI setup --
if [[ "$DO_SETUP" == "1" ]]; then
  echo "== [2/3] ComfyUI setup (clone + custom nodes + model downloads) =="
  echo "   (this downloads tens of GB and can take a long time)"
  python3 main.py --setup
else
  echo "== [2/3] Skipping ComfyUI setup (pass --setup for the full model install) =="
fi

# ----------------------------------------------------------- cloudflared --
if [[ "$TUNNEL" == "1" ]] && ! command -v cloudflared >/dev/null 2>&1; then
  echo "== Installing cloudflared =="
  BIN=/usr/local/bin/cloudflared
  if [[ $EUID -ne 0 ]]; then BIN="$HOME/.local/bin/cloudflared"; mkdir -p "$HOME/.local/bin"; fi
  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O "$BIN"
  chmod +x "$BIN"
  export PATH="$PATH:$(dirname "$BIN")"
fi

# -------------------------------------------------------------- arguments --
ARGS=()
if [[ "$MOCK" == "1" ]]; then
  echo "== [3/3] Starting ComfyRun server (MOCK: simulated jobs, no GPU required) =="
  ARGS+=(--mock)
else
  echo "== [3/3] Starting ComfyRun server (REAL: ComfyUI subprocess on 127.0.0.1:8188) =="
fi
if [[ "$TUNNEL" == "1" ]]; then
  ARGS+=(--tunnel)
fi

# ---------------------------------------------------------------- launch --
if [[ "$FG" == "1" || "$TUNNEL" == "0" ]]; then
  # Blocking run (user asked for foreground, or local-only without a URL).
  exec python3 main.py "${ARGS[@]}"
fi

# Background run: detach, then wait for the tunnel URL and print it.
if [[ -f "$PIDFILE" ]]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null || true)
  if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
    echo "Stopping previous background server (pid $OLD)..."
    kill "$OLD" 2>/dev/null || true
    sleep 2
  fi
fi

echo "Starting server in background (log: $(pwd)/$LOG)..."
nohup python3 main.py "${ARGS[@]}" > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "pid: $PID"

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
    echo "  stop:       bash run.sh stop"
    echo "======================================================="
    exit 0
  fi
  sleep 1
done

echo "Tunnel URL not seen after ${WAIT_SECONDS}s - last log lines:"
tail -n 30 "$LOG"
exit 1
