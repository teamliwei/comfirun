# ComfyRun Server

Headless ComfyUI orchestration server: FastAPI + strict FIFO job queue +
atomic manifest + Cloudflare quick tunnel + live WebSocket telemetry.
This repository is the server side only; pair it with the ComfyRun
CustomTkinter desktop client.

## Quick test on Colab (no GPU needed)

Clone, bootstrap, run — one cell:

```python
!git clone https://github.com/Luisjesch/cfrunnr.git
%cd /content/cfrunnr
!bash run.sh
```

That installs dependencies, installs `cloudflared`, and starts the server in
**mock mode** with a Cloudflare quick tunnel. Watch the log for:

```
Public tunnel URL: https://xxxx.trycloudflare.com
```

Point the ComfyRun desktop client at that URL and submit jobs — they are
simulated and return placeholder outputs, so you can verify the full
pipeline (queue, progress, results zip) end to end.

## Real generation (GPU runtime + models)

One-time setup (long: clones ComfyUI + 6 custom nodes, downloads the Qwen
Image 2.1 / SCAIL-2 model set):

```bash
bash run.sh --setup
```

Then run with the real ComfyUI bridge:

```bash
bash run.sh --real
```

`--real` spawns ComfyUI as a managed subprocess on `127.0.0.1:8188`
(`--lowvram`). Models stay warm between consecutive jobs of the same
workflow and are unloaded on workflow transitions.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | `available` / `busy` / `error` + queue depth |
| GET | `/workflows`, `/workflows/{id}` | Workflow metadata + parameter schema |
| POST | `/jobs` | Direct submission (409 if busy) |
| POST | `/queue-job` | FIFO submission (202) |
| GET | `/jobs`, `/jobs/{id}` | Job listing / status |
| GET | `/jobs/{id}/results` | Verified, zipped output archive |
| DELETE | `/jobs/{id}` | Cancel / interrupt |
| POST | `/free` | Memory management (`unload_models` flag) |
| WS | `/ws` | Logs, sampling progress, job signals |

## Configuration

Environment variables: `COMFYRUN_DATA_DIR`, `COMFYRUN_API_HOST`,
`COMFYRUN_API_PORT`, `COMFYRUN_COMFY_PORT`, `COMFYRUN_COMFY_DIR`,
`COMFYRUN_COMFY_ARGS`, `COMFYRUN_MOCK`, `HF_TOKEN`.

Runtime data lives under `data/` (manifest, logs, inputs, outputs, tmp) and
is never wiped by reruns; unfinished jobs are re-enqueued after a restart.
