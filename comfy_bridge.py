"""ComfyUI background process manager & internal API client.

ComfyUI runs as a managed persistent daemon subprocess (asyncio), fully
decoupled from the FastAPI event loop. This module handles:
  - subprocess spawn / readiness wait / stdout+stderr streaming
  - input file upload to ComfyUI's /upload/image endpoint
  - prompt submission (POST /prompt) and completion polling (GET /history/{id})
  - sampling progress relay from ComfyUI's internal WebSocket
  - smart model retention (POST /free with/without unload_models)
  - active generation cancellation (POST /interrupt)
  - output retrieval into the job's outputs directory
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import sys
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

import config
from logging_config import setup_logging

logger = setup_logging()

ProgressCallback = Callable[[int, int], Awaitable[None]]


class ComfyBridgeError(RuntimeError):
    pass


class ComfyBridge:
    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.client_id = uuid.uuid4().hex
        self._ready = False
        self._stop = False
        self._progress_task: Optional[asyncio.Task] = None
        # Latest progress snapshot per prompt_id: {"value": int, "max": int}
        self.progress: dict[str, dict] = {}
        self._log_file = None

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        """Spawn the ComfyUI daemon and wait until its API is ready."""
        if self.proc and self.proc.returncode is None:
            return
        comfy_dir = config.COMFY_DIR
        main_py = comfy_dir / "main.py"
        if not main_py.exists():
            raise ComfyBridgeError(
                f"ComfyUI not found at {comfy_dir}. Run `uv run main.py --setup` first."
            )

        config.COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        config.COMFY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)

        self._log_file = open(config.COMFYUI_LOG, "a", encoding="utf-8", errors="replace")
        cmd = [
            sys.executable,
            "main.py",
            "--listen",
            config.COMFY_HOST,
            "--port",
            str(config.COMFY_PORT),
            *config.COMFY_EXTRA_ARGS,
        ]
        logger.info("Spawning ComfyUI daemon: %s (cwd=%s)", " ".join(cmd), comfy_dir)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(comfy_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._stop = False
        asyncio.create_task(self._pump_stdout())
        await self.wait_ready()
        if self._progress_task is None or self._progress_task.done():
            self._progress_task = asyncio.create_task(self._listen_progress())

    async def _pump_stdout(self) -> None:
        """Read ComfyUI stdout line-by-line into comfyui.log and the WS relay."""
        import main as app_main  # late import to avoid a circular dependency

        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if self._log_file:
                    self._log_file.write(text + "\n")
                    self._log_file.flush()
                await app_main.ws_manager.broadcast(
                    {"type": "log", "source": "comfyui", "message": text}
                )
        except Exception as exc:
            logger.debug("comfy stdout pump ended: %s", exc)

    async def wait_ready(self, timeout: float = config.COMFY_READY_TIMEOUT) -> None:
        """Poll GET /history until the ComfyUI API answers."""
        async with httpx.AsyncClient(timeout=2.0) as client:
            waited = 0.0
            while waited < timeout:
                if self.proc and self.proc.returncode is not None:
                    raise ComfyBridgeError(
                        f"ComfyUI exited during startup (code {self.proc.returncode}). "
                        "See data/logs/comfyui.log"
                    )
                try:
                    resp = await client.get(f"{config.COMFY_BASE_URL}/history")
                    if resp.status_code == 200:
                        self._ready = True
                        logger.info("ComfyUI API is ready on port %s", config.COMFY_PORT)
                        return
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                waited += 1.0
            raise ComfyBridgeError("ComfyUI failed to become ready in time.")

    async def ensure_running(self) -> None:
        if not self.proc or self.proc.returncode is not None:
            logger.warning("ComfyUI daemon not running; respawning...")
            self._ready = False
            await self.start()
        elif not self._ready:
            await self.wait_ready()

    # ------------------------------------------------------- progress (WS) --

    async def _listen_progress(self) -> None:
        """Listen to ComfyUI's internal WebSocket and record sampling progress."""
        import websockets

        url = f"{config.COMFY_WS_URL}?clientId={self.client_id}"
        while not self._stop:
            try:
                async with websockets.connect(url, max_size=50 * 1024 * 1024) as ws:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        mtype = msg.get("type")
                        if mtype == "progress":
                            data = msg.get("data", {})
                            pid = data.get("prompt_id")
                            if pid:
                                self.progress[pid] = {
                                    "value": int(data.get("value", 0)),
                                    "max": int(data.get("max", 0)),
                                }
                        elif mtype == "execution_error":
                            data = msg.get("data", {})
                            logger.error(
                                "ComfyUI execution error: node=%s type=%s: %s",
                                data.get("node_id"),
                                data.get("exception_type"),
                                data.get("exception_message"),
                            )
            except asyncio.CancelledError:
                return
            except Exception:
                if not self._stop:
                    await asyncio.sleep(2.0)

    async def wait_for_progress(self, prompt_id: str, cb: ProgressCallback) -> None:
        """Invoke cb(value, max) whenever progress for prompt_id changes."""
        last = (-1, -1)
        while True:
            snap = self.progress.get(prompt_id)
            if snap and snap["max"] > 0 and (snap["value"], snap["max"]) != last:
                last = (snap["value"], snap["max"])
                await cb(last[0], last[1])
            if last[1] > 0 and last[0] >= last[1]:
                return
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------- actions --

    async def upload_input(self, path: Path, remote_name: str) -> str:
        """Upload a local file into ComfyUI's input directory. Returns remote name."""
        async with httpx.AsyncClient(timeout=600.0) as client:
            with open(path, "rb") as fh:
                resp = await client.post(
                    f"{config.COMFY_BASE_URL}/upload/image",
                    files={"image": (remote_name, fh, "application/octet-stream")},
                    data={"overwrite": "true", "type": "input"},
                )
        if resp.status_code != 200:
            raise ComfyBridgeError(f"Input upload failed: {resp.text[:300]}")
        data = resp.json()
        name = data.get("name", remote_name)
        subfolder = data.get("subfolder", "")
        return f"{subfolder}/{name}" if subfolder else name

    async def submit(self, graph: dict) -> str:
        """Submit a resolved prompt graph; returns the ComfyUI prompt_id."""
        await self.ensure_running()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{config.COMFY_BASE_URL}/prompt",
                json={"prompt": graph, "client_id": self.client_id},
            )
        if resp.status_code != 200:
            raise ComfyBridgeError(
                f"ComfyUI rejected the workflow: {resp.text[:1000]}"
            )
        prompt_id = resp.json()["prompt_id"]
        logger.info("Submitted prompt %s to ComfyUI", prompt_id)
        return prompt_id

    async def poll_history(self, prompt_id: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{config.COMFY_BASE_URL}/history/{prompt_id}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get(prompt_id)

    async def wait_for_completion(
        self,
        prompt_id: str,
        on_progress: Optional[ProgressCallback] = None,
        poll_seconds: float = 3.0,
    ) -> dict:
        """Poll /history until the prompt completes. Returns the history entry."""
        progress_task = (
            asyncio.create_task(self.wait_for_progress(prompt_id, on_progress))
            if on_progress
            else None
        )
        try:
            while True:
                if self.proc and self.proc.returncode is not None:
                    raise ComfyBridgeError("ComfyUI process crashed during generation.")
                entry = await self.poll_history(prompt_id)
                if entry is not None:
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        msgs = [
                            m.get("message", "")
                            for m in status.get("messages", [])
                            if m and m[0] == "execution_error"
                        ]
                        raise ComfyBridgeError(
                            f"ComfyUI execution error: {msgs or status}"
                        )
                    if entry.get("outputs") or status.get("completed"):
                        return entry
                await asyncio.sleep(poll_seconds)
        finally:
            if progress_task:
                progress_task.cancel()

    async def free_memory(self, unload_models: bool = False) -> None:
        """POST /free. unload_models=False keeps weights cached (warm retention)."""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                await client.post(
                    f"{config.COMFY_BASE_URL}/free",
                    json={"unload_models": unload_models, "free_memory": True},
                )
            logger.info("ComfyUI /free (unload_models=%s)", unload_models)
        except Exception as exc:
            logger.warning("POST /free failed: %s", exc)

    async def interrupt(self) -> None:
        """Abort the active generation without killing the daemon."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{config.COMFY_BASE_URL}/interrupt")
            logger.info("Sent /interrupt to ComfyUI")
        except Exception as exc:
            logger.warning("POST /interrupt failed: %s", exc)

    async def collect_outputs(self, history_entry: dict, dest_dir: Path) -> list[str]:
        """Copy output files produced by the prompt into dest_dir. Returns filenames."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for node_output in history_entry.get("outputs", {}).values():
            for kind in ("images", "gifs", "videos"):
                for item in node_output.get(kind, []) or []:
                    filename = item.get("filename")
                    if not filename:
                        continue
                    file_type = item.get("type", "output")
                    if file_type == "temp":
                        src = config.COMFY_DIR / "temp" / item.get("subfolder", "") / filename
                    elif file_type == "input":
                        src = config.COMFY_DIR / "input" / item.get("subfolder", "") / filename
                    else:
                        src = config.COMFY_OUTPUT_DIR / item.get("subfolder", "") / filename
                    if not src.exists():
                        logger.warning("Expected ComfyUI output missing: %s", src)
                        continue
                    target = dest_dir / filename
                    counter = 1
                    while target.exists():
                        target = dest_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                        counter += 1
                    await asyncio.to_thread(shutil.copyfile, src, target)
                    saved.append(target.name)
        return saved

    # ------------------------------------------------------------ shutdown --

    async def stop(self) -> None:
        self._stop = True
        if self._progress_task and not self._progress_task.done():
            self._progress_task.cancel()
        if self.proc and self.proc.returncode is None:
            logger.info("Terminating ComfyUI daemon...")
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        self.proc = None
        self._ready = False
