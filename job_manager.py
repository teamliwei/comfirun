"""Job manager: strict FIFO asyncio queue, job state machine, atomic manifest.

Single source of truth is data/manifest.json, written atomically (tempfile +
os.replace) under an asyncio.Lock on every mutation. Unfinished jobs are
re-enqueued on startup, so reruns after interruption never destroy progress.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from logging_config import setup_logging
from memory import purge_memory

logger = setup_logging()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex[:8]}"


@dataclass
class Job:
    id: str
    workflow_id: str
    params: dict = field(default_factory=dict)
    status: str = "pending"  # pending | processing | completed | error | cancelled
    created_at: str = field(default_factory=utcnow)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    inputs_path: str = ""
    outputs_path: str = ""
    expected_outputs: list = field(default_factory=list)
    output_type: str = "images"


def save_manifest(manifest: dict, manifest_path: Path) -> None:
    """Atomic manifest write: tempfile in the same dir, then os.replace."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(manifest_path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, default=str)
        os.replace(tmp_path, manifest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.manifest_lock = asyncio.Lock()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self.current_job_id: Optional[str] = None
        self.current_workflow_id: Optional[str] = None
        self.last_error: Optional[str] = None
        self._queue_membership: set[str] = set()
        self.cancel_requested: set[str] = set()

        # Injected collaborators (set by main.py on startup).
        self.comfy = None            # ComfyBridge
        self.ws_manager = None       # WebSocketManager (broadcast)

    # ------------------------------------------------------------- manifest --

    def load_manifest(self) -> None:
        if not config.MANIFEST_PATH.exists():
            return
        try:
            with open(config.MANIFEST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.error("Failed to read manifest (%s); starting fresh.", exc)
            return
        for raw in data.get("jobs", {}).values():
            job = Job(
                id=raw["id"],
                workflow_id=raw["workflow_id"],
                params=raw.get("params", {}),
                status=raw.get("status", "pending"),
                created_at=raw.get("created_at"),
                started_at=raw.get("started_at"),
                completed_at=raw.get("completed_at"),
                error_message=raw.get("error_message"),
                inputs_path=raw.get("inputs_path", ""),
                outputs_path=raw.get("outputs_path", ""),
                expected_outputs=raw.get("expected_outputs", []),
                output_type=raw.get("output_type", "images"),
            )
            self.jobs[job.id] = job

    async def restore_and_reenqueue(self) -> None:
        """Re-enqueue unfinished jobs after a restart (never destructive)."""
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at):
            if job.status in ("pending", "processing"):
                job.status = "pending"
                job.started_at = None
                self._queue_membership.add(job.id)
                await self.queue.put(job)
                logger.info("Re-enqueued unfinished job %s (%s)", job.id, job.workflow_id)
        await self.save_manifest_locked()

    async def save_manifest_locked(self) -> None:
        async with self.manifest_lock:
            manifest = {
                "version": 1,
                "jobs": {jid: asdict(job) for jid, job in self.jobs.items()},
            }
            await asyncio.to_thread(save_manifest, manifest, config.MANIFEST_PATH)

    # ------------------------------------------------------------ submission --

    def create_job(self, workflow_id: str, params: dict, output_type: str) -> Job:
        job_id = new_job_id()
        job = Job(
            id=job_id,
            workflow_id=workflow_id,
            params=params,
            output_type=output_type,
            inputs_path=f"inputs/{job_id}",
            outputs_path=f"outputs/{job_id}",
        )
        self.jobs[job.id] = job
        return job

    async def enqueue(self, job: Job) -> int:
        self._queue_membership.add(job.id)
        await self.queue.put(job)
        await self.save_manifest_locked()
        return self.queue.qsize()

    # ------------------------------------------------------------- properties --

    @property
    def is_processing(self) -> bool:
        return self.current_job_id is not None

    @property
    def has_error(self) -> bool:
        return self.last_error is not None and not self.is_processing

    def queue_position(self, job_id: str) -> int:
        items = list(self.queue._queue)  # asyncio.Queue internal deque (read-only use)
        for pos, job in enumerate(items, start=1):
            if getattr(job, "id", None) == job_id:
                return pos
        return 0

    # ------------------------------------------------------------- worker --

    async def start_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self.process_queue())

    async def process_queue(self) -> None:
        logger.info("FIFO queue worker started.")
        while True:
            job: Job = await self.queue.get()
            try:
                self._queue_membership.discard(job.id)
                if job.status == "cancelled" or job.id in self.cancel_requested:
                    await self._finish(job, "cancelled")
                    continue

                self.current_job_id = job.id
                job.status = "processing"
                job.started_at = utcnow()
                await self.save_manifest_locked()
                await self._broadcast(
                    {"type": "job_started", "job_id": job.id, "workflow_id": job.workflow_id}
                )
                logger.info("Processing job %s (%s)", job.id, job.workflow_id)

                try:
                    await self._execute_job(job)
                    job.status = "completed"
                    job.completed_at = utcnow()
                    await self._broadcast(
                        {
                            "type": "job_completed",
                            "job_id": job.id,
                            "output_type": job.output_type,
                            "outputs": job.expected_outputs,
                        }
                    )
                    logger.info("Job %s completed.", job.id)
                except _JobCancelled:
                    await self.comfy.interrupt()
                    await self._finish(job, "cancelled")
                except Exception as exc:
                    if job.id in self.cancel_requested:
                        # Failure was caused by the user's interrupt request.
                        await self._finish(job, "cancelled")
                    else:
                        job.status = "error"
                        job.error_message = str(exc)
                        job.completed_at = utcnow()
                        self.last_error = str(exc)
                        await self._broadcast(
                            {"type": "job_error", "job_id": job.id, "error": str(exc)}
                        )
                        logger.exception("Job %s failed: %s", job.id, exc)
            finally:
                self.current_job_id = None
                # Session cleanup only: models stay resident for consecutive
                # jobs of the same workflow (warm retention).
                if self.comfy:
                    await self.comfy.free_memory(unload_models=False)
                purge_memory()
                await self.save_manifest_locked()
                self.queue.task_done()

    async def _execute_job(self, job: Job) -> None:
        from workflow_registry import build_prompt_graph

        # Deep-purge only on workflow transition (cold start for new models).
        if self.current_workflow_id is not None and job.workflow_id != self.current_workflow_id:
            logger.info(
                "Workflow transition %s -> %s: unloading models.",
                self.current_workflow_id,
                job.workflow_id,
            )
            await self.comfy.free_memory(unload_models=True)
            purge_memory()
        self.current_workflow_id = job.workflow_id

        input_dir = config.DATA_DIR / job.inputs_path
        output_dir = config.DATA_DIR / job.outputs_path
        output_dir.mkdir(parents=True, exist_ok=True)

        # Map input slots (extensionless) to their stored filenames for
        # template injection: e.g. "image_1" -> "image_1.jpg".
        input_files = (
            {p.stem: p.name for p in sorted(input_dir.iterdir()) if p.is_file()}
            if input_dir.exists()
            else {}
        )

        graph = build_prompt_graph(job.workflow_id, job.params, input_files)
        prompt_id = await self.comfy.submit(graph)

        async def on_progress(value: int, maximum: int) -> None:
            pct = round(100.0 * value / maximum, 1) if maximum else 0.0
            await self._broadcast(
                {
                    "type": "progress",
                    "job_id": job.id,
                    "step": value,
                    "total_steps": maximum,
                    "percentage": pct,
                }
            )

        entry = await self.comfy.wait_for_completion(prompt_id, on_progress)
        self._check_cancel(job)

        saved = await self.comfy.collect_outputs(entry, output_dir)
        if not saved:
            raise RuntimeError("ComfyUI completed but produced no output files.")
        job.expected_outputs = sorted(saved)

    def _check_cancel(self, job: Job) -> None:
        if job.id in self.cancel_requested:
            raise _JobCancelled()

    async def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.completed_at = utcnow()
        await self._broadcast({"type": f"job_{status}", "job_id": job.id})

    async def _broadcast(self, message: dict) -> None:
        if self.ws_manager:
            await self.ws_manager.broadcast(message)

    # -------------------------------------------------------------- cancel --

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending/processing job. Returns False if not cancellable."""
        job = self.jobs.get(job_id)
        if not job or job.status in ("completed", "error", "cancelled"):
            return False
        self.cancel_requested.add(job_id)
        if job.status == "processing":
            # Active generation: interrupt ComfyUI immediately; the worker's
            # history poll observes the interrupt and marks the job cancelled.
            await self._interrupt_running(job_id)
        else:
            job.status = "cancelled"
            job.completed_at = utcnow()
            await self.save_manifest_locked()
            await self._broadcast({"type": "job_cancelled", "job_id": job_id})
        return True

    async def _interrupt_running(self, job_id: str) -> None:
        """Interrupt active ComfyUI generation and mark the job cancelled."""
        await self.comfy.interrupt()
        # The worker will observe the interrupt via history; poll briefly and
        # mark the job cancelled once it stops processing.
        for _ in range(30):
            job = self.jobs.get(job_id)
            if job and self.current_job_id != job_id:
                job.status = "cancelled"
                job.completed_at = utcnow()
                await self.save_manifest_locked()
                await self._broadcast({"type": "job_cancelled", "job_id": job_id})
                return
            await asyncio.sleep(1.0)

    def note_cancel_seen(self, job_id: str) -> None:
        """Worker-side hook: a running job was interrupted by user request."""
        job = self.jobs.get(job_id)
        if job:
            job.status = "cancelled"
            job.completed_at = utcnow()


class _JobCancelled(Exception):
    pass
