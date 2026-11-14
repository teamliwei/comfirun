#!/usr/bin/env bash
# ComfyRun server SETUP for Colab / fresh Linux VMs. One-time, long-running.
#
# Prepares the environment only - it never starts a server and never opens a
# tunnel. Starting the server is done by run.sh, separately.
#
# What it installs:
#   [1/3] Server Python dependencies (requirements.txt)
#   [2/3] cloudflared binary (for run.sh --tunnel)
#   [3/3] ComfyUI + 6 custom nodes + the Qwen Image 2.1 / SCAIL-2 model set
#         (tens of GB - takes a very long time on the first run)
#
# Rerunning is safe: already-installed parts are skipped or refreshed.
# Usage:
#   bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== ComfyRun server setup =="
echo "python: $(python3 --version)"

echo "== [1/3] Installing server dependencies =="
python3 -m pip install -q -r requirements.txt

echo "== [2/3] Installing cloudflared =="
if command -v cloudflared >/dev/null 2>&1; then
  echo "   already installed: $(command -v cloudflared)"
else
  BIN=/usr/local/bin/cloudflared
  if [[ $EUID -ne 0 ]]; then
    BIN="$HOME/.local/bin/cloudflared"
    mkdir -p "$HOME/.local/bin"
  fi
  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O "$BIN"
  chmod +x "$BIN"
  export PATH="$PATH:$(dirname "$BIN")"
  echo "   installed: $BIN"
fi

echo "== [3/3] ComfyUI + custom nodes + models (very long) =="
python3 main.py --setup

echo ""
echo "Setup complete. Start the server with: bash run.sh"
