#!/usr/bin/env bash
# ComfyRun server bootstrap for Colab / fresh Linux VMs.
#
# Installs dependencies, optionally prepares ComfyUI + models, then starts
# the server with a Cloudflare quick tunnel so a remote client can connect.
#
# Usage:
#   bash run.sh                  # FAST: install deps + mock server + tunnel
#   bash run.sh --setup          # one-time: clone ComfyUI, custom nodes, download models (LONG)
#   bash run.sh --real           # real ComfyUI bridge (requires --setup done once + GPU)
#   bash run.sh --no-tunnel      # bind locally only (no cloudflared)
#
# Modes (default = mock):
#   mock  - simulates jobs, generates placeholder outputs. No GPU/ComfyUI needed.
#   real  - spawns ComfyUI as a subprocess on 127.0.0.1:8188. Requires the
#           model setup from --setup and a CUDA-capable machine.
set -euo pipefail
cd "$(dirname "$0")"

DO_SETUP=0
MOCK=1
TUNNEL=1
for arg in "$@"; do
  case "$arg" in
    --setup)    DO_SETUP=1 ;;
    --real)     MOCK=0 ;;
    --no-tunnel) TUNNEL=0 ;;
    *) echo "Unknown flag: $arg"; exit 1 ;;
  esac
done

echo "== ComfyRun server bootstrap =="
echo "python: $(python3 --version)"

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

# ------------------------------------------------------------------- run --
echo "== [3/3] Starting ComfyRun server =="
ARGS=()
if [[ "$MOCK" == "1" ]]; then
  echo "   mode: MOCK (simulated jobs, no GPU required)"
  ARGS+=(--mock)
else
  echo "   mode: REAL (ComfyUI subprocess on 127.0.0.1:8188)"
fi
if [[ "$TUNNEL" == "1" ]]; then
  echo "   tunnel: enabled (public trycloudflare.com URL will be printed)"
  ARGS+=(--tunnel)
else
  echo "   tunnel: disabled"
fi

exec python3 main.py "${ARGS[@]}"
