# ComfyRun Server - Colab Execution Guide

How to run the ComfyRun headless server in a Google Colab notebook.

Two separate scripts, two separate phases:

| Script | Phase | Duration | Opens a server? |
|--------|-------|----------|-----------------|
| `server/setup.sh` | one-time environment preparation | very long (tens of GB of models) | never |
| `server/run.sh` | start / stop the server | seconds to minutes | yes |

`setup.sh` never starts a server and never opens a tunnel. `run.sh` never
downloads or installs anything. The server URL is printed only after the API
actually answers requests - never while initialization is still running.

## 1. Fresh runtime - clone the repository

```python
%cd /content
!git clone https://github.com/teamliwei/comfirun.git
%cd /content/comfirun/server
```

## 2. Existing runtime - update to the latest version

```python
%cd /content/comfirun/server
!git pull
```

Always run `git pull` before starting the server if the runtime was used
before. Fixes are shipped to the repository, not to your runtime.

## 3. One-time setup: `bash setup.sh`

```python
!bash setup.sh
```

What it does, in order:

1. Installs the server Python dependencies.
2. Installs the `cloudflared` binary.
3. Clones ComfyUI plus six custom nodes and downloads the Qwen Image 2.1 /
   SCAIL-2 model set (tens of GB).

This takes a very long time on the first run. When the cell finishes, the
environment is ready - nothing is listening, no URL exists yet.

## 4. Start the server: `bash run.sh`

```python
!bash run.sh
```

What happens on start:

- The server and the ComfyUI subprocess it manages run in the background,
  fully detached from the notebook cell.
- The cell returns and prints the public Cloudflare URL ONLY when the API
  is answering requests:

```
=======================================================
  SERVER IS UP (running in background)
  URL:  https://xxxx.trycloudflare.com
=======================================================
```

- The URL is created by the server itself after initialization completes.
  If the cell shows "waiting for the API to answer /status", the server is
  still initializing - do not start the client yet.
- The tunnel hostname changes on every restart. After restarting the
  server, use the new URL with the client.

## 5. Connect the desktop client

On your Windows machine:

```
run_client.bat https://xxxx.trycloudflare.com
```

The URL parameter auto-connects and auto-loads the workflow form. Connect
only after the `SERVER IS UP` block appeared.

## 6. Stop the server

```python
!bash run.sh stop
```

Stops the background server and the tunnel.

## 7. Logs

| What | Where |
|------|-------|
| Server console output (setup runs append here too) | `server.log` |
| FastAPI request/job log | `data/logs/fastapi.log` |
| ComfyUI subprocess output | `data/logs/comfyui.log` |
| Cloudflare tunnel output | `data/logs/cloudflared.log` |

View the tail of the server log from a cell:

```python
!tail -n 50 server.log
```

## 8. Common issues

| Symptom | Cause and fix |
|---------|---------------|
| "server dependencies are not installed" | `run.sh` was started before `setup.sh`. Run `!bash setup.sh` once, then start the server. |
| "ComfyUI is not installed here" | `run.sh` was started before setup. Run `!bash setup.sh` once, then `!bash run.sh`. |
| "cloudflared is not installed" | Setup step 2 did not run on this runtime. Run `!bash setup.sh` (or at minimum reinstall cloudflared). |
| `SERVER PROCESS EXITED during startup` | The server failed while initializing. Read the reason: `!tail -n 30 server.log`. |
| Cell waits at "waiting for the API to answer /status" | Normal during real-mode initialization (model loading can take minutes). The server is not yet accepting connections; wait for the URL block. If it never appears, check `data/logs/comfyui.log`. |
| Job error mentioning an unknown node | The workflow needs a recent ComfyUI and its custom nodes. Re-run `!bash setup.sh`, and check `data/logs/comfyui.log` for the exact node name. |
| Job error mentioning CUDA / out of memory | The runtime GPU is too small for the selected workflow. SCAIL-2 (14B parameters) needs a large-memory GPU runtime. |
| Client cannot connect | Tunnel hostname changed after a server restart, or the client was started before the `SERVER IS UP` block. Use the URL from the latest block. |
| Port already in use | A previous server instance is still running: `!bash run.sh stop`, then start again. |
| Model download shows a warning | Non-fatal unless the workflow using it is needed. `rife47.pth` is downloaded automatically by ComfyUI-Frame-Interpolation on first use. |

## 9. Requirements

| GPU | Setup | Use |
|-----|-------|-----|
| required (large VRAM for SCAIL-2) | `bash setup.sh` once | actual generation |
