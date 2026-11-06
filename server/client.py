"""Build the three API requests and return BVH bytes; no Blender state is read."""

import json
import math
import secrets

from ..common.config import Config
from ..core.generation_models import GenerationError
from ..core.storage import Storage
from .jobs import AnimationJobs


def get_fps() -> float:
    return Config.DEFAULT_FPS


def _build_animation_name(prompt: str) -> str:
    return f"blender_{'_'.join(prompt.split())[:15] or 'animation'}"


def get_animation(
    name=None,
    prompt="",
    duration=5.0,
    temperature=1.0,
    model="gen2",
    in_place=False,
    cycled=False,
    seed=-1,
    enhance_prompt: bool = True,
    *,
    api_token=None,
    fps=Config.DEFAULT_FPS,
    cancel_event=None,
    on_progress=None,
    source='blender'
) -> bytes:
    if not isinstance(prompt, str) or not prompt.strip():
        raise GenerationError("Text generation requires a prompt.")
    if not all(type(value) in (int, float) and math.isfinite(value)
               for value in (duration, fps, temperature)):
        raise GenerationError("Duration, FPS and temperature must be finite.")
    if duration <= 0 or fps <= 0 or temperature <= 0:
        raise GenerationError("Duration, FPS and temperature must be positive.")
    if type(seed) is not int or seed < -1:
        raise GenerationError("seed must be -1 or a non-negative integer.")
    jobs = AnimationJobs()
    token = Storage.api_token if api_token is None else api_token
    payload = {
        "prompt": prompt,
        "prompt_duration": duration,
        "prompt_fps": fps,
        "prompt_temperature": temperature,
        "name": name or _build_animation_name(prompt),
        "api_token": token,
        "model": model or Config.DEFAULT_MODEL_TYPE,
        "cycled": cycled,
        "seed": secrets.randbelow(2**31 - 1) + 1 if seed == -1 else seed,
        "enhance_prompt": enhance_prompt,
        "source": source
    }
    return jobs.generate_animation(
        Config.GENERATION_URL,
        api_token=token,
        in_place=in_place,
        cancel_event=cancel_event,
        on_progress=on_progress,
        json=payload
    )


def get_animation_constraints(
    name=None,
    prompt="",
    num_frames=150,
    pose_constraints=None,
    motion_files=None,
    seed=-1,
    text_weight=2.0,
    constraint_weight=2.5,
    first_heading=0.0,
    direction=None,
    model="gen2",
    in_place=False,
    cycled=False,
    enhance_prompt: bool = True,
    *,
    end_effector_targets=None,
    api_token=None,
    cancel_event=None,
    on_progress=None,
    source='blender'
) -> bytes:
    if type(num_frames) is not int or num_frames <= 0 or type(seed) is not int or seed < -1:
        raise GenerationError("num_frames must be positive and seed must be -1 or non-negative integers.")
    if not all(math.isfinite(value) for value in (text_weight, constraint_weight, first_heading)):
        raise GenerationError("Constraint weights and heading must be finite.")
    if direction is not None and (
        len(direction) != 3 or not all(math.isfinite(value) for value in direction)
        or math.hypot(*direction) <= 1.0e-8
    ):
        raise GenerationError("direction must contain three finite values and be non-zero.")
    constraints, payloads = tuple(pose_constraints or ()), tuple(motion_files or ())
    if len(constraints) != len(payloads):
        raise GenerationError("Each pose constraint must have exactly one motion file.")
    normalized = []
    for index, (constraint, payload) in enumerate(zip(constraints, payloads)):
        kind = constraint.get("constraint_type", "")
        source_frame = constraint.get("source_frame", 0)
        target_frame = constraint.get("target_frame", 0)
        joints = constraint.get("joint_names") or []
        if kind not in Config.CONSTRAINT_TYPES:
            raise GenerationError("Unknown constraint type.")
        if type(source_frame) is not int or source_frame < 0 or type(target_frame) is not int:
            raise GenerationError("Constraint frame indices must be non-negative integers.")
        if not 0 <= target_frame < num_frames:
            raise GenerationError("Constraint target frame exceeds num_frames.")
        if not isinstance(payload, bytes) or not payload:
            raise GenerationError("Constraint motion files must contain non-empty NPZ bytes.")
        if kind == "end-effector" and (
            not joints or any(joint not in Config.END_EFFECTOR_JOINTS for joint in joints)
        ):
            raise GenerationError("end-effector requires supported joint names.")
        item = {
            "motion_file_index": index, "source_frame": source_frame, "target_frame": target_frame,
            "constraint_type": kind,
        }
        if kind == "end-effector":
            item["joint_names"] = list(joints)
        normalized.append(item)
    targets = _normalize_effector_targets(
        end_effector_targets or (), num_frames,
        {item["target_frame"] for item in normalized},
    )
    if targets and (cycled or in_place):
        raise GenerationError("End effector targets currently require Cycled and In Place to be disabled.")
    metadata = {
        "text_prompt": prompt, 
        "bvh_filename": name or _build_animation_name(prompt),
        "model": model or Config.DEFAULT_MODEL_TYPE, 
        "num_frames": num_frames,
        "direction": direction, 
        "pose_constraints": normalized,
        "seed": secrets.randbelow(2**31 - 1) + 1 if seed == -1 else seed, 
        "text_weight": text_weight,
        "constraint_weight": constraint_weight, 
        "first_heading": first_heading, 
        "cycled": cycled,
        "enhance_prompt": enhance_prompt,
        "source": source
    }
    # Omit optional lists when unused: the existing pose_constraints field
    # requires a nonempty list whenever it is present.
    if not normalized:
        metadata.pop("pose_constraints")
    if targets:
        metadata["end_effector_targets"] = targets
    token = Storage.api_token if api_token is None else api_token
    files = [
        ("api_token", (None, token)),
        ("metadata", (None, json.dumps(metadata, ensure_ascii=False))),
    ]
    files.extend(
        ("motion_files", (f"constraint_{index + 1}.npz", payload, "application/octet-stream"))
        for index, payload in enumerate(payloads)
    )
    jobs = AnimationJobs()
    return jobs.generate_animation(
        Config.GENERATION_CONSTRAINTS_URL,
        api_token=token, 
        in_place=in_place, 
        cancel_event=cancel_event,
        on_progress=on_progress, 
        files=files,
    )


def _normalize_effector_targets(targets, num_frames, pose_frames):
    normalized, seen, roots = [], set(), {}
    for target in targets:
        joint = target.get("joint_name")
        frame = target.get("target_frame")
        if joint not in {"LeftHand", "RightHand", "LeftFoot", "RightFoot"}:
            raise GenerationError("End effector target requires a supported hand or foot.")
        if type(frame) is not int or not 0 <= frame < num_frames:
            raise GenerationError("End effector target frame must be inside the Clip.")
        if frame in pose_frames or (joint, frame) in seen:
            raise GenerationError("End effector target conflicts with another constraint on this frame.")
        seen.add((joint, frame))
        item = {"joint_name": joint, "target_frame": frame}
        for name, length in (("position", 3), ("rotation", 4), ("root_2d", 2)):
            values = target.get(name)
            if not isinstance(values, (list, tuple)) or len(values) != length or not all(
                type(value) in (int, float) and math.isfinite(value) for value in values
            ):
                raise GenerationError(f"Target {name} must contain {length} finite numbers.")
            item[name] = list(values)
        norm = math.hypot(*item["rotation"])
        if not math.isfinite(norm) or norm <= 1.0e-8:
            raise GenerationError("Target rotation must be a nonzero finite WXYZ quaternion.")
        item["rotation"] = [value / norm for value in item["rotation"]]
        previous = roots.setdefault(frame, item["root_2d"])
        if any(abs(a - b) > 1.0e-5 for a, b in zip(previous, item["root_2d"])):
            raise GenerationError("Targets on the same frame must share the root reference.")
        normalized.append(item)
    return normalized


def get_animation_inbetween(
    left_context_pose,
    right_context_pose,
    name=None,
    prompt="",
    left_frame=0,
    right_frame=0,
    left_context_frames=1,
    right_context_frames=1,
    inbetween_frames=150,
    seed=-1,
    model="gen2",
    in_place=False,
    end_offset_x=0.0,
    end_offset_z=0.0,
    align_heading=True,
    *,
    api_token=None,
    cancel_event=None,
    on_progress=None,
    source='blender'
) -> bytes:
    if any(type(value) is not int or value < 0 for value in (left_frame, right_frame)) or (
        type(seed) is not int or seed < -1
    ):
        raise GenerationError("Context indices must be non-negative and seed must be -1 or non-negative.")
    if any(type(value) is not int or value <= 0 for value in (
        left_context_frames, right_context_frames, inbetween_frames
    )):
        raise GenerationError("Context and gap frame counts must be positive integers.")
    if not all(math.isfinite(value) for value in (end_offset_x, end_offset_z)):
        raise GenerationError("Inbetween offsets must be finite.")
    if any(not isinstance(payload, bytes) or not payload
           for payload in (left_context_pose, right_context_pose)):
        raise GenerationError("Both contexts must contain non-empty NPZ bytes.")
    metadata = {
        "text_prompt": prompt,
        "bvh_filename": name or _build_animation_name(prompt),
        "model": model or Config.DEFAULT_MODEL_TYPE,
        "left_frame": left_frame,
        "right_frame": right_frame,
        "left_context_frames": left_context_frames,
        "right_context_frames": right_context_frames,
        "gap_frames": inbetween_frames,
        "seed": secrets.randbelow(2**31 - 1) + 1 if seed == -1 else seed,
        "end_offset_x": end_offset_x,
        "end_offset_z": end_offset_z,
        "align_heading": align_heading,
        "source": source,
    }
    token = Storage.api_token if api_token is None else api_token
    jobs = AnimationJobs()
    return jobs.generate_animation(
        Config.INBETWEENING_URL,
        api_token=token, 
        in_place=in_place,
        cancel_event=cancel_event,
        on_progress=on_progress,
        data={
            "api_token": token, 
            "metadata": json.dumps(metadata, ensure_ascii=False)
        },
        files={
            "left_motion": ("left_context_pose.npz", left_context_pose, "application/octet-stream"),
            "right_motion": ("right_context_pose.npz", right_context_pose, "application/octet-stream"),
        },
    )
