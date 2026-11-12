"""ComfyRun server entrypoint.

FastAPI app exposing:
  GET    /status                 server state (available / busy / error) + queue depth
  GET    /workflows              list available workflows
  GET    /workflows/{id}         full parameter schema for one workflow
  POST   /jobs                   direct submission (409 + queue suggestion if busy)
  POST   /queue-job              FIFO queue submission (always accepted, 202)
  GET    /jobs                   list jobs
  GET    /jobs/{id}              job status + metadata
  GET    /jobs/{id}/results      verified, zipped output archive (streamed)
  DELETE /jobs/{id}              cancel pending / interrupt running job
  WS     /ws                     live logs, sampling progress, job signals

Run:
  uv run main.py --setup     # first run: clone ComfyUI, install deps, download models
  uv run main.py --tunnel    # FastAPI + Cloudflare quick tunnel
  uv run main.py             # local-only
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

import config
from comfy_bridge import ComfyBridge
from job_manager import JobManager
from logging_config import setup_logging
from tunnel import TunnelManager
from workflow_registry import get_workflow, list_workflows

logger = setup_logging()

app = FastAPI(title="ComfyRun", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_bridge(mock: bool):
    """Pick the real ComfyUI bridge or the mock (GPU-less) bridge."""
    if mock:
        from mock_comfy import MockComfyBridge

        return MockComfyBridge()
    return ComfyBridge()


USE_MOCK = os.getenv("COMFYRUN_MOCK", "").strip().lower() in ("1", "true", "yes")
comfy = _build_bridge(USE_MOCK)
job_manager = JobManager()
tunnel = TunnelManager()
job_manager.comfy = comfy


class WebSocketManager:
    """Broadcast hub for /ws clients (logs, progress, job signals)."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        logger.info("WebSocket client connected (%s total)", len(self.clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            targets = list(self.clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


ws_manager = WebSocketManager()
job_manager.ws_manager = ws_manager

SKIP_COMFY = False  # set by --skip-comfy


@app.on_event("startup")
async def startup() -> None:
    config.ensure_directories()
    config.ensure_manifest()
    job_manager.load_manifest()
    await job_manager.restore_and_reenqueue()
    await job_manager.start_worker()
    if not SKIP_COMFY:
        try:
            await comfy.start()
        except Exception as exc:
            # Server still boots: jobs can be queued; ComfyUI respawns lazily.
            logger.error("ComfyUI startup deferred: %s", exc)
    logger.info("ComfyRun server ready on port %s", config.API_PORT)


@app.on_event("shutdown")
async def shutdown() -> None:
    tunnel.stop()
    await comfy.stop()


# ----------------------------------------------------------------- helpers --


async def parse_upload_form(request) -> tuple[str, str, list[tuple[str, UploadFile]]]:
    """Parse a multipart form: (workflow_id, params_json, [(field_name, file)]).

    FastAPI's named `list[UploadFile]` params only collect files posted under
    that exact field name, so we read the raw form and keep each file's
    multipart key as its field name.
    """
    form = await request.form()
    workflow_id = str(form.get("workflow_id", ""))
    params = str(form.get("params", "{}"))
    uploads: list[tuple[str, UploadFile]] = []
    for key, value in form.multi_items():
        if hasattr(value, "filename") and hasattr(value, "read"):
            uploads.append((key, value))
    return workflow_id, params, uploads


async def stream_upload(file: UploadFile, dest: Path) -> None:
    """Stream an UploadFile to disk chunk-by-chunk (no full-RAM buffering)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(config.CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)


def validate_submission(
    workflow_id: str, params: str, uploads: list[tuple[str, UploadFile]]
):
    try:
        spec = get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_id}")

    import json as _json

    try:
        params_dict = _json.loads(params) if params else {}
    except Exception:
        raise HTTPException(status_code=400, detail="params must be a JSON object")

    # Required input files must be present (matched by field name or filename).
    field_names = {field for field, _f in uploads}
    filenames = {(f.filename or "") for _field, f in uploads}
    for req in spec["input_files"]:
        if not req["required"]:
            continue
        name = req["name"]
        hit = name in field_names or name in filenames or any(
            fn.startswith(name) for fn in filenames
        )
        if not hit:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required input file '{name}' ({req['label']})",
            )
    return spec, params_dict


def slot_name_for(field_name: str, filename: str, spec: dict) -> Optional[str]:
    """Match an uploaded file to the workflow input slot it fills."""
    slots = {s["name"] for s in spec["input_files"]}
    if field_name in slots:
        return field_name
    for slot in sorted(slots, key=len, reverse=True):
        if slot in (filename or "") or (filename or "").startswith(slot):
            return slot
    return None


async def handle_submission(
    workflow_id: str, params: str, uploads: list[tuple[str, UploadFile]]
) -> dict:
    spec, params_dict = validate_submission(workflow_id, params, uploads)
    job = job_manager.create_job(workflow_id, params_dict, spec["output_type"])
    input_dir = config.DATA_DIR / job.inputs_path

    for field_name, upload in uploads:
        slot = slot_name_for(field_name, upload.filename or "", spec)
        if slot is None:
            logger.warning("Ignoring unmatched upload: %s (%s)", upload.filename, field_name)
            continue
        base = Path(upload.filename or slot)
        if base.name.startswith(slot):
            # Filename already carries the slot name (e.g. headless_ref.png):
            # keep it verbatim so ComfyUI LoadImage finds it by that name.
            dest = input_dir / base.name
        else:
            ext = base.suffix or Path(slot).suffix or ".bin"
            dest = input_dir / f"{slot}{ext}"
        await stream_upload(upload, dest)
        logger.info("Job %s: stored input %s -> %s", job.id, upload.filename, dest.name)

    position = await job_manager.enqueue(job)
    return {"job": job, "queue_position": position}


def zip_outputs(job_id: str, outputs_path: Path) -> Path:
    zip_path = config.TMP_DIR / f"{job_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(outputs_path.iterdir()):
            if f.is_file():
                zf.write(f, arcname=f.name)
    return zip_path


# ---------------------------------------------------------------- endpoints --


@app.get("/status")
async def get_status():
    if job_manager.has_error:
        return {"status": "error", "message": job_manager.last_error}
    if job_manager.is_processing:
        return {
            "status": "busy",
            "current_job": job_manager.current_job_id,
            "current_workflow": job_manager.current_workflow_id,
            "queue_length": job_manager.queue.qsize(),
        }
    return {
        "status": "available",
        "current_workflow": job_manager.current_workflow_id,
        "queue_length": job_manager.queue.qsize(),
        "tunnel_url": tunnel.url,
    }


@app.get("/workflows")
async def workflows_list():
    return {"workflows": list_workflows()}


@app.get("/workflows/{workflow_id}")
async def workflow_detail(workflow_id: str):
    try:
        return get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_id}")


@app.post("/jobs")
async def submit_job(request: Request):
    workflow_id, params, uploads = await parse_upload_form(request)
    if job_manager.is_processing:
        raise HTTPException(
            status_code=409,
            detail=(
                "Server is currently busy processing another job. "
                "Use POST /queue-job to queue your request."
            ),
        )
    result = await handle_submission(workflow_id, params, uploads)
    return {
        "job_id": result["job"].id,
        "status": result["job"].status,
        "queue_position": result["queue_position"],
    }


@app.post("/queue-job")
async def queue_job(request: Request):
    """Always accepted: streams inputs to disk and enqueues into the FIFO queue."""
    workflow_id, params, uploads = await parse_upload_form(request)
    result = await handle_submission(workflow_id, params, uploads)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": result["job"].id,
            "status": "pending",
            "queue_position": result["queue_position"],
        },
    )


@app.get("/jobs")
async def list_jobs(status: Optional[str] = None):
    jobs = [j for j in job_manager.jobs.values() if status is None or j.status == status]
    jobs.sort(key=lambda j: j.created_at)
    from dataclasses import asdict

    return {"jobs": [asdict(j) for j in jobs]}


@app.get("/jobs/{job_id}")
async def job_detail(job_id: str):
    from dataclasses import asdict

    job = job_manager.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    data = asdict(job)
    if job.status == "pending" and not job_manager.is_processing:
        data["queue_position"] = job_manager.queue_position(job_id)
    return data


@app.get("/jobs/{job_id}/results")
async def job_results(job_id: str):
    job = job_manager.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Job is not finished yet")

    outputs_dir = config.DATA_DIR / job.outputs_path
    # Atomic delivery: verify expected outputs exist and are non-empty.
    if job.expected_outputs:
        missing = [
            f
            for f in job.expected_outputs
            if not (outputs_dir / f).exists() or (outputs_dir / f).stat().st_size == 0
        ]
        if missing:
            raise HTTPException(
                status_code=500,
                detail=f"Output files incomplete - generation may have failed: {missing}",
            )
    elif not outputs_dir.exists() or not any(outputs_dir.iterdir()):
        raise HTTPException(status_code=500, detail="No output files found for this job")

    zip_path = zip_outputs(job_id, outputs_dir)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{job_id}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    success = await job_manager.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or already completed")
    return {"job_id": job_id, "status": "cancelled"}


@app.post("/free")
async def free_memory(unload_models: bool = False):
    await comfy.free_memory(unload_models=unload_models)
    return {"status": "ok", "unload_models": unload_models}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            # Clients do not push state over WS; we just keep the socket alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await ws_manager.disconnect(ws)


# -------------------------------------------------------------------- main --


def main() -> None:
    global comfy, USE_MOCK
    parser = argparse.ArgumentParser(description="ComfyRun headless server")
    parser.add_argument("--setup", action="store_true", help="Run first-time setup (clone ComfyUI, download models)")
    parser.add_argument("--tunnel", action="store_true", help="Expose the server via a Cloudflare quick tunnel")
    parser.add_argument("--mock", action="store_true", help="Run without ComfyUI: simulate jobs with placeholder outputs (no GPU needed)")
    parser.add_argument("--port", type=int, default=config.API_PORT)
    parser.add_argument("--host", type=str, default=config.API_HOST)
    parser.add_argument("--skip-comfy", action="store_true", help="Do not spawn the ComfyUI daemon")
    args = parser.parse_args()

    config.ensure_directories()
    config.ensure_manifest()

    if args.setup:
        import setup as setup_mod

        setup_mod.run_setup()

    if args.mock and not USE_MOCK:
        USE_MOCK = True
        comfy = _build_bridge(True)
        job_manager.comfy = comfy
        logger.info("Running in MOCK mode (--mock): jobs are simulated.")

    if args.tunnel:
        tunnel.start(
            port=args.port,
            on_url=lambda url: logger.info("Public tunnel URL: %s", url),
            log_path=config.CLOUDFLARED_LOG,
        )

    global SKIP_COMFY
    SKIP_COMFY = args.skip_comfy
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
