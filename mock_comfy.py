"""Mock ComfyUI bridge for GPU-less environments and development.

Drop-in replacement for ComfyBridge with the same async interface. Simulates
the full job lifecycle without ComfyUI, torch, or a GPU:

  - submit(): accepts the resolved prompt graph (records it for inspection)
  - wait_for_completion(): streams realistic sampling progress over several
    seconds, then reports a history entry with output files
  - collect_outputs(): writes a generated placeholder PNG (qwen) or MP4
    placeholder (scail2) into the job's outputs directory
  - interrupt(): cancels the in-flight simulated run

Enabled with `uv run main.py --mock` (or COMFYRUN_MOCK=1).
"""
from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from logging_config import setup_logging

logger = setup_logging()

# Simulated sampling wall-time per job (seconds).
MOCK_RUN_SECONDS = 6.0


class MockComfyInterrupted(Exception):
    pass


class MockComfyBridge:
    def __init__(self) -> None:
        self.client_id = "mock"
        self.last_graph: dict | None = None
        self._interrupt_event = asyncio.Event()
        self.progress: dict[str, dict] = {}
        self._started = False

    # ------------------------------------------------------------- lifecycle --

    async def start(self) -> None:
        self._started = True
        logger.info("MOCK ComfyBridge active (no ComfyUI required).")

    async def ensure_running(self) -> None:
        if not self._started:
            await self.start()

    async def stop(self) -> None:
        self._started = False

    async def wait_ready(self, timeout: float = 5.0) -> None:
        return None

    # -------------------------------------------------------------- actions --

    async def upload_input(self, path: Path, remote_name: str) -> str:
        return remote_name

    async def submit(self, graph: dict) -> str:
        await self.ensure_running()
        self.last_graph = graph
        prompt_id = f"mock-{time.time_ns()}"
        logger.info("MOCK submit: prompt_id=%s", prompt_id)
        return prompt_id

    async def poll_history(self, prompt_id: str):
        return None

    async def wait_for_completion(self, prompt_id: str, on_progress=None, poll_seconds: float = 1.0) -> dict:
        steps = 12
        self._interrupt_event.clear()
        delay = MOCK_RUN_SECONDS / steps
        for step in range(1, steps + 1):
            # Pause instead of busy-wait so cancels are responsive.
            try:
                await asyncio.wait_for(self._interrupt_event.wait(), timeout=delay)
                raise MockComfyInterrupted()
            except asyncio.TimeoutError:
                pass
            if on_progress:
                await on_progress(step, steps)
        return {
            "outputs": {
                "mock": {"images": [{"filename": "", "type": "output", "_mock": True}]}
            }
        }

    async def free_memory(self, unload_models: bool = False) -> None:
        logger.info("MOCK /free (unload_models=%s)", unload_models)

    async def interrupt(self) -> None:
        self._interrupt_event.set()

    async def collect_outputs(self, history_entry: dict, dest_dir: Path) -> list[str]:
        """Produce placeholder outputs matching each workflow's output type."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        graph = self.last_graph or {}
        saved: list[str] = []
        try:
            if "461" in graph:
                # qwen_image_edit: a procedurally generated placeholder image.
                from PIL import Image, ImageDraw

                res = int(graph["474"]["inputs"].get("resolution", 1024))
                img = Image.new("RGB", (min(res, 512), min(res, 512)), (24, 34, 52))
                draw = ImageDraw.Draw(img)
                for i in range(0, img.width, 32):
                    draw.line([(i, 0), (i, img.height)], fill=(35, 48, 70))
                seed = graph["458"]["inputs"].get("seed", 0)
                draw.text(
                    (12, 12),
                    f"ComfyRun MOCK output\nseed={seed}",
                    fill=(240, 240, 240),
                )
                name = f"Qwen_image_2.1_{random.randint(1, 99999):05d}_.png"
                img.save(dest_dir / name)
                saved.append(name)
            else:
                # scail2_i2v: placeholder video container (no playback data).
                name = f"Wan21_SCAIL2_Headless_{random.randint(1, 99999):05d}.mp4"
                payload = b"\x00\x00\x00\x18ftypmock\x00\x00\x00\x00mock" + b"\x00" * 4096
                (dest_dir / name).write_bytes(payload)
                saved.append(name)
        except Exception as exc:
            raise RuntimeError(f"MOCK output generation failed: {exc}")
        logger.info("MOCK produced outputs: %s", saved)
        return saved
