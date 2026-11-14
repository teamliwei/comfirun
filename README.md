# ComfyRun Server

Headless ComfyUI orchestration server: FastAPI + strict FIFO job queue +
atomic manifest + Cloudflare quick tunnel + live WebSocket telemetry.
This repository is the server side only; pair it with the ComfyRun
CustomTkinter desktop client.

## Running on Colab

Clone, install, run. `run.sh` only starts the server; it installs nothing.

```python
!git clone https://github.com/teamliwei/comfirun.git
%cd /content/comfirun
!bash setup.sh        # one-time: dependencies + cloudflared + ComfyUI + models
!bash run.sh          # start the real server (needs a GPU runtime)
```

The server starts **in the background** and the cell returns only when the
API answers requests — the URL block is never printed while the server is
still initializing:

```
=======================================================
  SERVER IS UP (running in background)
  URL:  https://xxxx.trycloudflare.com
=======================================================
```

Point the ComfyRun desktop client at that URL and submit real jobs.

Useful follow-ups:
- `!tail -n 50 server.log` — live server logs
- `bash run.sh stop` — stop the background server

## Real generation (GPU runtime + models)

One-time setup (long: clones ComfyUI + 6 custom nodes, downloads the Qwen
Image 2.1 / SCAIL-2 model set). `setup.sh` never starts a server:

```bash
bash setup.sh
```

Then run — startup blocks until ComfyUI's API is ready before accepting
requests, and the tunnel URL appears only after that:

```bash
bash run.sh
```

`run.sh` spawns ComfyUI as a managed subprocess on `127.0.0.1:8188`
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
`COMFYRUN_COMFY_ARGS`, `HF_TOKEN`.

Runtime data lives under `data/` (manifest, logs, inputs, outputs, tmp) and
is never wiped by reruns; unfinished jobs are re-enqueued after a restart.
