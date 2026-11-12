"""Per-component structured logging for the ComfyRun server.

Three log files under data/logs/:
  - fastapi.log     : FastAPI request/app logs (Python logging, RotatingFileHandler)
  - comfyui.log     : ComfyUI subprocess stdout/stderr (written by comfy_bridge)
  - cloudflared.log : Cloudflare tunnel process output (written by tunnel)
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import FASTAPI_LOG, LOG_DIR

_configured = False


def setup_logging(log_dir: Path = LOG_DIR) -> logging.Logger:
    """Configure the 'comfyrun' application logger. Safe to call multiple times."""
    global _configured
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("comfyrun")
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(FASTAPI_LOG, maxBytes=10_000_000, backupCount=3)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    logger.addHandler(handler)

    # Mirror app logs to stdout for foreground runs.
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
    logger.addHandler(console)

    logger.propagate = False
    _configured = True
    return logger
