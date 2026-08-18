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


def get_animation_constraints(
    name: str | None = None,
    prompt: str = "",
    num_frames: int = 150,
    pose_constraints: list[dict] | None = None,
    motion_files: list[bytes] | None = None,
    seed: int = 42,
    text_weight: float = 2.0,
    constraint_weight: float = 2.5,
    first_heading: float = 0.0,
    direction: list[float] | None = None,
    model: str = "gen2",
    in_place: bool = False,
):
    """Send generation input and zero or more captured pose constraints."""

    num_frames = int(num_frames)
    if num_frames <= 0:
        raise ValueError("num_frames must be positive.")

    raw_pose_constraints = list(pose_constraints or [])
    motion_payloads = list(motion_files or [])
    if len(raw_pose_constraints) != len(motion_payloads):
        raise ValueError("Each pose constraint must have exactly one motion file.")

    normalized_pose_constraints: list[dict] = []
    for index, raw_constraint in enumerate(raw_pose_constraints):
        constraint_type = str(raw_constraint.get("constraint_type", ""))
        if constraint_type not in config.CONSTRAINT_TYPES:
            raise ValueError(
                f"Invalid constraint_type: {constraint_type}. "
                f"Must be one of {sorted(config.CONSTRAINT_TYPES)}."
            )

        source_frame = int(raw_constraint.get("source_frame", 0))
        target_frame = int(raw_constraint.get("target_frame", 0))
        if source_frame < 0:
            raise ValueError("source_frame must be non-negative.")
        if not 0 <= target_frame < num_frames:
            raise ValueError(f"target_frame must be between 0 and {num_frames - 1}.")

        joint_names = list(raw_constraint.get("joint_names") or [])
        if constraint_type == "end-effector":
            invalid_joint_names = [
                joint_name
                for joint_name in joint_names
                if joint_name not in config.END_EFFECTOR_JOINTS
            ]
            if not joint_names or invalid_joint_names:
                raise ValueError(
                    f"Invalid joint_names: {joint_names}. "
                    "Select one or more values from "
                    f"{sorted(config.END_EFFECTOR_JOINTS)}."
                )
        else:
            joint_names = []

        payload = motion_payloads[index]
        if not isinstance(payload, bytes) or not payload:
            raise ValueError(f"motion_files[{index}] must contain NPZ bytes.")

        normalized_constraint = {
            "motion_file_index": index,
            "source_frame": source_frame,
            "target_frame": target_frame,
            "constraint_type": constraint_type,
        }
        if joint_names:
            normalized_constraint["joint_names"] = joint_names
        normalized_pose_constraints.append(normalized_constraint)

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
        "pose_constraints": normalized_pose_constraints,
        "seed": seed,
        "text_weight": text_weight,
        "constraint_weight": constraint_weight,
        "first_heading": first_heading,
    }
    data = {
        "api_token": Storage.api_token,
        "extension": "bvh",
        "include_skin": False,
        "in_place": bool(in_place),
        "metadata": json.dumps(
            metadata,
            ensure_ascii=False,
        ),
    }
    files = [
        (
            "motion_files",
            (
                f"constraint_{index + 1}.npz",
                payload,
                "application/octet-stream",
            ),
        )
        for index, payload in enumerate(motion_payloads)
    ]

    try:
        response = requests.post(
            config.GENERATION_CONSTRAINTS_URL,
            data=data,
            files=files or None,
            timeout=config.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.error(f"Animation request with constraints failed: {exc}")
        raise RuntimeError("Failed to fetch animation with constraints from server.") from exc
    return response.content


def get_animation_inbetween(
    left_context_pose: bytes,
    right_context_pose: bytes,
    name: str | None = None,
    prompt: str = "",
    left_frame: int = 0,
    right_frame: int = 0,
    left_context_frames: int = 1,
    right_context_frames: int = 1,
    inbetween_frames: int = 150,
    seed: int = 42,
    model: str = "gen2",
    in_place: bool = False,
    end_offset_x: float = 0.0,
    end_offset_z: float = 0.0,
    align_heading: bool = True,
):
    """send prompt to server for inbetweening task, get animation back"""

    left_frame = int(left_frame)
    right_frame = int(right_frame)
    left_context_frames = int(left_context_frames)
    right_context_frames = int(right_context_frames)
    inbetween_frames = int(inbetween_frames)

    if inbetween_frames <= 0:
        raise ValueError("inbetween_frames must be positive.")

    if left_context_frames <= 0:
        raise ValueError("left_context_frames must be positive.")
    if right_context_frames <= 0:
        raise ValueError("right_context_frames must be positive.")
    if left_frame < 0 or right_frame < 0:
        raise ValueError("left_frame and right_frame must be non-negative.")

    for label, value in (
        ("end_offset_x", end_offset_x),
        ("end_offset_z", end_offset_z),
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
        "left_frame": left_frame,
        "left_context_frames": left_context_frames,
        "right_frame": right_frame,
        "right_context_frames": right_context_frames,
        "gap_frames": inbetween_frames,
        "end_offset_x": end_offset_x,
        "end_offset_z": end_offset_z,
        "align_heading": align_heading,
        "seed": seed,
    }

    data = {
        "api_token": Storage.api_token,
        "extension": "bvh",
        "include_skin": False,
        "in_place": bool(in_place),
        "metadata": json.dumps(
            metadata,
            ensure_ascii=False,
        ),
    }

    files = {
        "left_motion": (
            "left_context_pose.npz",
            left_context_pose,
            "application/octet-stream",
        ),
        "right_motion": (
            "right_context_pose.npz",
            right_context_pose,
            "application/octet-stream",
        ),
    }

    try:
        response = requests.post(
            config.INBETWEENING_URL,
            data=data,
            files=files,
            timeout=config.REQUEST_TIMEOUT,
        )

        response.raise_for_status()
    except requests.RequestException as exc:
        log.error(f"Inbetween animation request failed: {exc}")
        raise RuntimeError("Failed to fetch inbetween animation from server.") from exc
    return response.content
