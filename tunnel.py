"""Cloudflare Quick Tunnel manager.

Launches `cloudflared tunnel --url http://127.0.0.1:<port>`, extracts the
public trycloudflare.com URL from stderr, saves it to data/tunnel_url.txt and
tees all output to data/logs/cloudflared.log.
"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

import config

TUNNEL_URL_RE = re.compile(r"https://[\w-]+\.trycloudflare\.com")


class TunnelManager:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.url: Optional[str] = None
        self._lock = threading.Lock()

    def start(self, port: int, on_url: Callable[[str], None], log_path: Path) -> None:
        """Start the quick tunnel in a daemon thread; calls on_url(url) when ready."""

        def _run() -> None:
            try:
                with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
                    self.proc = subprocess.Popen(
                        [
                            "cloudflared",
                            "tunnel",
                            "--url",
                            f"http://127.0.0.1:{port}",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    assert self.proc.stdout is not None
                    for line in self.proc.stdout:
                        log_file.write(line)
                        log_file.flush()
                        match = TUNNEL_URL_RE.search(line)
                        if match:
                            url = match.group(0)
                            with self._lock:
                                self.url = url
                            config.TUNNEL_URL_PATH.write_text(url, encoding="utf-8")
                            try:
                                on_url(url)
                            except Exception:
                                pass
            except FileNotFoundError:
                with open(log_path, "a", encoding="utf-8") as log_file:
                    log_file.write(
                        "cloudflared executable not found on PATH; "
                        "install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
                    )
            except Exception as exc:  # keep the thread alive on unexpected errors
                try:
                    with open(log_path, "a", encoding="utf-8") as log_file:
                        log_file.write(f"tunnel thread error: {exc}\n")
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True, name="comfyrun-tunnel").start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None
        self.url = None
