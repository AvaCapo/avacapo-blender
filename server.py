import json
import math

import bpy
import requests

from .storage import Storage
from .logger import log
from .config import Config
from .models import get_default_model_type, get_models_names

config = Config()


def get_fps() -> int:
    """get fps from blender please"""
    try:
        return bpy.context.scene.render.fps
    except:
        return config.DEFAULT_FPS


def _build_animation_name(prompt: str) -> str:
    normalized_prompt = "_".join(prompt.split())[:15]
    return f"blender_{normalized_prompt or 'animation'}"


def get_animation(
    name: str | None = None,
    prompt: str = "",
    duration: float = 5.0,
    temperature: float = 1.0,
    model: str = "",
    in_place: bool = False,
):
    """send prompt to server, get animation back"""
    model_names = get_models_names()
    if not model:
        model = get_default_model_type()
    if model not in model_names:
        message = f"Invalid model type: {model}. Must be one of {model_names}."
        log.error(message)
        raise ValueError(message)

    payload = {
        "prompt": prompt,
        "prompt_duration": duration,
        "prompt_fps": get_fps(),
        "prompt_temperature": temperature,
        "name": name or _build_animation_name(prompt),
        "api_token": Storage.api_token,
        "model": model,
        "extension": "bvh",
        "include_skin": False,
        "in_place": in_place,
    }
    safe_payload = {**payload, "api_token": "***" if payload["api_token"] else ""}
    log.info("Sending request with payload: %s", safe_payload)

    try:
        response = requests.post(config.GENERATION_URL, json=payload, timeout=config.REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.error(f"Animation request failed: {exc}")
        raise RuntimeError("Failed to fetch animation from server.") from exc
        
    return response.content


def get_animation_constraints(
    name: str | None = None,
    prompt: str = "",
    num_frames: int = 150,
    constraint_type: str = "fullbody",
    source_frame: int = 0,
    target_frame: int | None = None,
    joint_name: list[str] | None = None,
    seed: int = 42,
    text_weight: float = 2.0,
    constraint_weight: float = 2.5,
    first_heading: float = 0.0,
    direction: list[float] | None = None,
    constraint_pose: bytes | None = None,
    model: str = "gen2",
):
    """send prompt to server with constraints, get animation back"""

    num_frames = int(num_frames)
    source_frame = int(source_frame)
    if num_frames <= 0:
        raise ValueError("num_frames must be positive.")
    if constraint_type not in config.CONSTRAINT_TYPES:
        raise ValueError(
            f"Invalid constraint_type: {constraint_type}. "
            f"Must be one of {sorted(config.CONSTRAINT_TYPES)}."
        )
    joint_names = list(joint_name or [])

    if constraint_type == "end-effector":
        invalid_joint_names = [
            name for name in joint_names if name not in config.END_EFFECTOR_JOINTS
        ]
        if not joint_names or invalid_joint_names:
            raise ValueError(
                f"Invalid joint_name: {joint_names}. "
                "Select one or more values from "
                f"{sorted(config.END_EFFECTOR_JOINTS)}."
            )
    else:
        joint_names = []

    if (target_frame is None) and (constraint_pose is not None):
        raise ValueError("target_frame must be provided when constraint_pose is given.")
    if (constraint_pose is None) and (target_frame is not None):
        raise ValueError("constraint_pose must be provided when target_frame is given.")
    if constraint_pose is not None:
        target_frame = int(target_frame)
        if source_frame < 0:
            raise ValueError("source_frame must be non-negative.")
        if not 0 <= target_frame < num_frames:
            raise ValueError(
                f"target_frame must be between 0 and {num_frames - 1}."
            )
    if direction is not None:
        direction = [float(value) for value in direction]
        if len(direction) != 3 or not all(math.isfinite(value) for value in direction):
            raise ValueError("direction must contain three finite values.")
        if math.sqrt(sum(value * value for value in direction)) <= 1.0e-8:
            raise ValueError("direction cannot be zero.")

    for label, value in (
        ("text_weight", text_weight),
        ("constraint_weight", constraint_weight),
        ("first_heading", first_heading),
    ):
        if not math.isfinite(float(value)):
            raise ValueError(f"{label} must be finite.")

    model_names = get_models_names()
    if not model:
        model = get_default_model_type()
    if model not in model_names:
        message = f"Invalid model type: {model}. Must be one of {model_names}."
        log.error(message)
        raise ValueError(message)

    metadata = {
        "text_prompt": prompt,
        "bvh_filename": name or _build_animation_name(prompt),
        "model": model,
        "num_frames": num_frames,
        "direction": direction,
        "constraint_type": constraint_type,
        "source_frame": source_frame,
        "target_frame": target_frame,
        "joint_names": joint_names,
        "seed": seed,
        "text_weight": text_weight,
        "constraint_weight": constraint_weight,
        "first_heading": first_heading,
    }
    data = {
        "api_token": Storage.api_token,
        "extension": "bvh",
        "include_skin": False,
        "metadata": json.dumps(
            metadata,
            ensure_ascii=False,
        ),
    }
    if constraint_pose is not None:
        files = {
            "motion_file": (
                "pose.npz",
                constraint_pose,
                "application/octet-stream",
            ),
        }
    else:
        files = None
    try:
        response = requests.post(
            config.GENERATION_CONSTRAINTS_URL,
            data=data,
            files=files,
            timeout=config.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.error(f"Animation request with constraints failed: {exc}")
        raise RuntimeError("Failed to fetch animation with constraints from server.") from exc
    return response.content
