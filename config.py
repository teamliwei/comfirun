"""ComfyRun server configuration: paths, ports, environment."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Data root is overridable (COMFYRUN_DATA_DIR) so tests/dev runs can isolate
# their state in a temp folder; default stays alongside the server package.
DATA_DIR = Path(os.getenv("COMFYRUN_DATA_DIR", str(BASE_DIR / "data")))
LOG_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"
INPUTS_DIR = DATA_DIR / "inputs"
OUTPUTS_DIR = DATA_DIR / "outputs"
MANIFEST_PATH = DATA_DIR / "manifest.json"
TUNNEL_URL_PATH = DATA_DIR / "tunnel_url.txt"

FASTAPI_LOG = LOG_DIR / "fastapi.log"
COMFYUI_LOG = LOG_DIR / "comfyui.log"
CLOUDFLARED_LOG = LOG_DIR / "cloudflared.log"

# FastAPI is the public-facing API (tunneled); ComfyUI stays internal-only.
API_HOST = os.getenv("COMFYRUN_API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("COMFYRUN_API_PORT", "8000"))

COMFY_HOST = "127.0.0.1"
COMFY_PORT = int(os.getenv("COMFYRUN_COMFY_PORT", "8188"))
COMFY_BASE_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_WS_URL = f"ws://{COMFY_HOST}:{COMFY_PORT}/ws"

# Location of the ComfyUI checkout (setup.py clones it here).
COMFY_DIR = Path(os.getenv("COMFYRUN_COMFY_DIR", str(BASE_DIR / "ComfyUI")))
COMFY_INPUT_DIR = COMFY_DIR / "input"
COMFY_OUTPUT_DIR = COMFY_DIR / "output"

# Extra flags for the ComfyUI subprocess (SCAIL-2 needs --lowvram).
COMFY_EXTRA_ARGS = [
    a for a in os.getenv("COMFYRUN_COMFY_ARGS", "--lowvram").split() if a
]

# Byte size for streaming uploads / downloads (64KB, per implementation plan).
CHUNK_SIZE = 64 * 1024

# Startup readiness timeout for the ComfyUI subprocess (seconds).
COMFY_READY_TIMEOUT = 600


def ensure_directories() -> None:
    """Create the pregenerated data directory tree (idempotent, never destructive)."""
    for d in (DATA_DIR, LOG_DIR, TMP_DIR, INPUTS_DIR, OUTPUTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_manifest() -> None:
    """Create an empty manifest.json if none exists (never overwrites existing data)."""
    if not MANIFEST_PATH.exists():
        MANIFEST_PATH.write_text(json_dumps({"version": 1, "jobs": {}}), encoding="utf-8")


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)
