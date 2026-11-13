"""First-time ComfyRun server setup: ComfyUI clone, custom nodes, model downloads.

Merged from the two source colab scripts:
  - docs/qwenimage_un(1).txt   (NoctQ / Qwen Image 2.1 models)
  - docs/scail2_colab_only.txt (SCAIL-2 custom nodes + Wan2.1/SAM3/RIFE models)

Idempotent: existing checkouts and model files are skipped, never deleted.
No torch is installed here; ComfyUI's own requirements bring its runtime deps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import config

COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"

# SCAIL-2 custom nodes (from scail2_colab_only.txt).
CUSTOM_NODES = [
    ("ComfyUI-GGUF", "https://github.com/city96/ComfyUI-GGUF.git"),
    ("ComfyUI-VideoHelperSuite", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git"),
    ("ComfyUI-KJNodes", "https://github.com/kijai/ComfyUI-KJNodes.git"),
    ("rgthree-comfy", "https://github.com/rgthree/rgthree-comfy.git"),
    ("ComfyUI-KikoTools", "https://github.com/ComfyAssets/ComfyUI-KikoTools.git"),
    ("ComfyUI-Frame-Interpolation", "https://github.com/Fannovel16/ComfyUI-Frame-Interpolation.git"),
]

# (repo_id, remote_path, destination subfolder relative to ComfyUI dir)
QWEN_MODELS = [
    ("Comfy-Org/Qwen-Image-2.1", "text_encoders/qwen3vl_8b_int8_convrot.safetensors", "models/text_encoders"),
    ("Comfy-Org/Qwen-Image-2.1", "vae/qwen_image_2.1_vae_bf16.safetensors", "models/vae"),
    ("Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1", "NoctQ_V4_int8_convrot.safetensors", "models/diffusion_models"),
]

SCAIL2_MODELS = [
    ("realrebelai/SCAIL-2_GGUF", "SCAIL-2-Q4_K_M.gguf", "models/unet"),
    ("Comfy-Org/sam3.1", "checkpoints/sam3.1_multiplex_fp16.safetensors", "models/checkpoints"),
    (
        "lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v",
        "loras/Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
        "models/loras",
    ),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "models/text_encoders"),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/clip_vision/clip_vision_h.safetensors", "models/clip_vision"),
    ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/vae/wan_2.1_vae.safetensors", "models/vae"),
    ("AlexWortega/RIFE", "rife47.pth", "custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife"),
]


def _run(cmd: list, **kwargs) -> None:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def setup_repo(comfy_dir: Path) -> None:
    """Clone or update ComfyUI and install its requirements."""
    if not (comfy_dir / "main.py").exists():
        print(f"Cloning ComfyUI into {comfy_dir} ...")
        _run(["git", "clone", COMFYUI_REPO, str(comfy_dir)])
    else:
        print(f"ComfyUI already present at {comfy_dir} (skipping clone).")

    print("Installing ComfyUI requirements...")
    _run([sys.executable, "-m", "pip", "install", "-r", str(comfy_dir / "requirements.txt")])


def install_custom_nodes(comfy_dir: Path) -> None:
    nodes_dir = comfy_dir / "custom_nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    for name, repo in CUSTOM_NODES:
        target = nodes_dir / name
        if target.exists():
            print(f"Custom node {name} already present (skipping).")
            continue
        print(f"Cloning custom node {name}...")
        _run(["git", "clone", "--recursive", repo, str(target)])
        req = target / "requirements.txt"
        if req.exists():
            _run([sys.executable, "-m", "pip", "install", "-r", str(req)])


def download_models(comfy_dir: Path) -> None:
    """Fetch all workflow models via huggingface_hub into exact target paths."""
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    from huggingface_hub import hf_hub_download

    token = os.getenv("HF_TOKEN", "").strip() or None

    for repo_id, remote_path, dest_subpath in QWEN_MODELS + SCAIL2_MODELS:
        dest_dir = comfy_dir / dest_subpath
        dest_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(remote_path).name
        target_path = dest_dir / filename
        if target_path.exists():
            print(f"Skipping {filename} (already exists).")
            continue
        print(f"Downloading {filename} from {repo_id} ...")
        try:
            cached = hf_hub_download(repo_id=repo_id, filename=remote_path, token=token)
            # hf_hub_download may nest under repo subfolders; flatten to target.
            nested = dest_dir / remote_path
            if nested.exists() and nested != target_path:
                shutil.move(str(nested), str(target_path))
                shutil.rmtree(str(dest_dir / Path(remote_path).parts[0]), ignore_errors=True)
            elif Path(cached) != target_path:
                shutil.copyfile(cached, target_path)
            print(f"Installed {filename} -> {dest_subpath}/")
        except Exception as exc:
            note = ""
            if filename == "rife47.pth":
                note = (
                    " (non-fatal: ComfyUI-Frame-Interpolation downloads RIFE "
                    "weights automatically on first use)"
                )
            print(f"WARNING: failed to download {filename}: {exc}{note}")


def run_setup() -> None:
    config.ensure_directories()
    comfy_dir = config.COMFY_DIR
    print("=== ComfyRun setup ===")
    print(f"[1/3] ComfyUI repository -> {comfy_dir}")
    setup_repo(comfy_dir)
    print("[2/3] Custom nodes")
    install_custom_nodes(comfy_dir)
    print("[3/3] Models (this can take a long time)")
    download_models(comfy_dir)
    print("Setup complete. Start the server with: uv run main.py [--tunnel]")


if __name__ == "__main__":
    run_setup()
