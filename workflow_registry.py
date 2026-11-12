"""Workflow registry: metadata, parameter schemas, and template injection.

Each workflow is defined by:
  1. An API-format JSON template in server/workflows/
  2. A metadata descriptor (name, description, param schema, input files, output type)

build_prompt_graph() substitutes client-provided parameters into a deep copy of
the template and returns the fully-resolved prompt graph ready for ComfyUI
POST /prompt.
"""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

from config import BASE_DIR

WORKFLOWS_DIR = BASE_DIR / "workflows"

SEED_NONE = -1  # client form value meaning "randomize"


def _load_template(filename: str) -> dict:
    with open(WORKFLOWS_DIR / filename, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.pop("_comment", None)
    return data


def _qwen_batch_slot(image_index: int) -> str:
    """BatchImagesNode input-slot name for the Nth reference image.

    Faithful to the original workflow: image_1 -> image0, image_2 -> image1,
    image_3 -> image5, image_4 -> image6, ... image_10 -> image12.
    """
    if image_index == 1:
        return "image0"
    if image_index == 2:
        return "image1"
    return f"image{image_index + 2}"


def _build_qwen_graph(graph: dict, params: dict, input_files: dict[str, str]) -> dict:
    """Inject Qwen Image Edit parameters.

    input_files maps slot name ("image_1".."image_10") -> uploaded filename in
    ComfyUI's input/ directory. LoadImage nodes ("img_N") are created
    dynamically for every provided slot and wired into both the prompt encoder
    (node 474) and the prompt-refine batcher (node 485).
    """
    seed = params.get("seed", SEED_NONE)
    if seed == SEED_NONE or seed is None:
        seed = random.randint(1, 2**53)
    seed = int(seed)

    graph["458"]["inputs"]["seed"] = seed
    graph["458"]["inputs"]["steps"] = int(params.get("steps", 12))
    graph["458"]["inputs"]["cfg"] = float(params.get("cfg", 1.0))
    graph["458"]["inputs"]["sampler_name"] = params.get("sampler", "euler")
    graph["458"]["inputs"]["scheduler"] = params.get("scheduler", "simple")

    graph["474"]["inputs"]["negative_prompt"] = params.get(
        "negative_prompt", "blurry, ugly, deformed"
    )
    graph["474"]["inputs"]["resolution"] = int(params.get("resolution", 1024))

    custom_size = bool(params.get("custom_size", False))
    graph["468"]["inputs"]["switch"] = custom_size
    if custom_size:
        graph["456"]["inputs"]["width"] = int(params.get("width", 1024))
        graph["456"]["inputs"]["height"] = int(params.get("height", 1024))

    prompt = str(params.get("positive_prompt", ""))
    refine = bool(params.get("refine_prompt", False))
    graph["484"]["inputs"]["on_false"] = prompt
    graph["484"]["inputs"]["switch"] = refine
    graph["500"]["inputs"]["prompt"] = prompt
    graph["500"]["inputs"]["sampling_mode.seed"] = seed

    # Wipe any statically-defined image slots, then wire provided images.
    for key in list(graph["474"]["inputs"]):
        if key.startswith("image_"):
            del graph["474"]["inputs"][key]
    for key in list(graph["485"]["inputs"]):
        if key.startswith("image"):
            del graph["485"]["inputs"][key]

    for slot in sorted(input_files):
        idx = int(slot.split("_")[-1])
        graph[f"img_{idx}"] = {
            "class_type": "LoadImage",
            "inputs": {"image": input_files[slot]},
        }
        graph["474"]["inputs"][f"image_{idx}"] = [f"img_{idx}", 0]
        graph["485"]["inputs"][_qwen_batch_slot(idx)] = [f"img_{idx}", 0]

    return graph


def _build_scail2_graph(graph: dict, params: dict, input_files: dict[str, str]) -> dict:
    """Inject SCAIL-2 I2V parameters."""
    seed = params.get("seed", SEED_NONE)
    if seed == SEED_NONE or seed is None:
        seed = random.randint(1, 4294967295)
    seed = int(seed)

    frame_count = int(params.get("frame_count", 65))
    rife_mult = int(params.get("rife_mult", 3))
    select_step = 3 if frame_count >= 65 else 2

    graph["179"]["inputs"]["text"] = str(params.get("prompt_text", ""))
    graph["87"]["inputs"]["text"] = str(params.get("tracking_prompt", "person"))
    graph["33"]["inputs"]["frame_load_cap"] = frame_count
    graph["33"]["inputs"]["select_every_nth"] = select_step
    # Input files are keyed by slot stem ("headless_ref" / "headless_drive").
    graph["33"]["inputs"]["video"] = input_files.get(
        "headless_drive", "headless_drive.mp4"
    )
    graph["178"]["inputs"]["image"] = input_files.get(
        "headless_ref", "headless_ref.png"
    )
    graph["114"]["inputs"]["length"] = frame_count
    graph["184"]["inputs"]["multiplier"] = rife_mult
    graph["19"]["inputs"]["noise_seed"] = seed
    return graph


WORKFLOW_REGISTRY: dict[str, dict[str, Any]] = {
    "qwen_image_edit": {
        "name": "Qwen Image 2.1 - Image Edit (NoctQ)",
        "description": (
            "Image editing/generation using Qwen Image 2.1 with the NoctQ V4 model. "
            "Supports up to 10 reference images, prompt-based editing, and an "
            "optional LLM prompt refinement pass."
        ),
        "template": "qwen_image_edit.json",
        "builder": _build_qwen_graph,
        "params": [
            {"name": "positive_prompt", "type": "string", "required": True,
             "widget": "textarea", "label": "Positive Prompt", "default": ""},
            {"name": "negative_prompt", "type": "string", "required": False,
             "widget": "textarea", "label": "Negative Prompt",
             "default": "blurry, ugly, deformed"},
            {"name": "resolution", "type": "int", "default": 1024, "min": 512,
             "max": 2048, "step": 64, "label": "Resolution (pixel budget)"},
            {"name": "steps", "type": "int", "default": 12, "min": 1, "max": 50,
             "label": "Steps"},
            {"name": "cfg", "type": "float", "default": 1.0, "min": 0.0,
             "max": 20.0, "step": 0.1, "label": "CFG"},
            {"name": "sampler", "type": "combo", "default": "euler",
             "options": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_sde"],
             "label": "Sampler"},
            {"name": "scheduler", "type": "combo", "default": "simple",
             "options": ["simple", "normal", "karras", "sgm_uniform"],
             "label": "Scheduler"},
            {"name": "seed", "type": "int", "default": SEED_NONE, "min": -1,
             "max": 2**53, "seed": True, "label": "Seed (-1 = random)"},
            {"name": "refine_prompt", "type": "bool", "default": False,
             "label": "Refine Prompt (PE model)"},
            {"name": "custom_size", "type": "bool", "default": False,
             "label": "Custom Size"},
            {"name": "width", "type": "int", "default": 1024, "min": 256,
             "max": 2048, "step": 16, "label": "Width (custom size)"},
            {"name": "height", "type": "int", "default": 1024, "min": 256,
             "max": 2048, "step": 16, "label": "Height (custom size)"},
        ],
        "input_files": [
            {"name": f"image_{i}", "required": (i == 1), "label": f"Image {i}",
             "extensions": [".png", ".jpg", ".jpeg", ".webp"]}
            for i in range(1, 11)
        ],
        "output_type": "images",
        "output_prefix": "Qwen_image_2.1",
        "models": {
            "unet": "NoctQ_V4_int8_convrot.safetensors",
            "clip": "qwen3vl_8b_int8_convrot.safetensors",
            "vae": "qwen_image_2.1_vae_bf16.safetensors",
        },
    },
    "scail2_i2v": {
        "name": "SCAIL-2 - Image-to-Video",
        "description": (
            "Character-driven image-to-video generation using SCAIL-2 with a "
            "Wan 2.1 I2V backbone. SAM3 tracks the character, RIFE interpolates "
            "frames. Reference image + driving motion video required."
        ),
        "template": "scail2_i2v.json",
        "builder": _build_scail2_graph,
        "params": [
            {"name": "prompt_text", "type": "string", "required": True,
             "widget": "textarea", "label": "Scene Prompt", "default": ""},
            {"name": "tracking_prompt", "type": "string", "required": True,
             "widget": "entry", "label": "SAM3 Tracking Prompt",
             "default": "person"},
            {"name": "frame_count", "type": "int", "default": 65, "min": 17,
             "max": 129, "step": 2, "label": "Base Frame Count"},
            {"name": "rife_mult", "type": "int", "default": 3, "min": 1,
             "max": 4, "label": "RIFE Interpolation Multiplier"},
            {"name": "seed", "type": "int", "default": SEED_NONE, "min": -1,
             "max": 4294967295, "seed": True, "label": "Seed (-1 = random)"},
        ],
        "input_files": [
            {"name": "headless_ref.png", "required": True,
             "label": "Reference Character Image",
             "extensions": [".png", ".jpg", ".jpeg"]},
            {"name": "headless_drive.mp4", "required": True,
             "label": "Driving Motion Video", "extensions": [".mp4", ".webm"]},
        ],
        "output_type": "video",
        "output_prefix": "Wan21_SCAIL2_Headless",
        "models": {
            "unet": "SCAIL-2-Q4_K_M.gguf",
            "checkpoint": "sam3.1_multiplex_fp16.safetensors",
            "lora": "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
            "text_encoder": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "clip_vision": "clip_vision_h.safetensors",
            "vae": "wan_2.1_vae.safetensors",
        },
        "custom_nodes": [
            "ComfyUI-GGUF",
            "ComfyUI-VideoHelperSuite",
            "ComfyUI-KJNodes",
            "rgthree-comfy",
            "ComfyUI-KikoTools",
            "ComfyUI-Frame-Interpolation",
        ],
    },
}


def list_workflows() -> list[dict]:
    """Public metadata for GET /workflows."""
    out = []
    for wid, spec in WORKFLOW_REGISTRY.items():
        out.append(
            {
                "id": wid,
                "name": spec["name"],
                "description": spec["description"],
                "params": spec["params"],
                "input_files": spec["input_files"],
                "output_type": spec["output_type"],
            }
        )
    return out


def get_workflow(workflow_id: str) -> dict:
    if workflow_id not in WORKFLOW_REGISTRY:
        raise KeyError(workflow_id)
    spec = WORKFLOW_REGISTRY[workflow_id]
    return {
        "id": workflow_id,
        "name": spec["name"],
        "description": spec["description"],
        "params": spec["params"],
        "input_files": spec["input_files"],
        "output_type": spec["output_type"],
    }


def build_prompt_graph(
    workflow_id: str, params: dict, input_files: dict[str, str]
) -> dict:
    """Resolve a workflow template into a final prompt graph for POST /prompt.

    input_files maps the workflow's input-file slot names to the filenames the
    files were stored under inside ComfyUI's input/ directory.
    """
    if workflow_id not in WORKFLOW_REGISTRY:
        raise KeyError(f"Unknown workflow: {workflow_id}")
    spec = WORKFLOW_REGISTRY[workflow_id]
    graph = copy.deepcopy(_load_template(spec["template"]))
    return spec["builder"](graph, params or {}, input_files or {})
