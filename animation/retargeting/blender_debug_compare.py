"""DEBUG ONLY: compare direct BVH loading with Blender Action extraction.

This module deliberately stops before retargeting.  It writes numerical FK
evidence for the two source representations so coordinate-frame issues can be
located without inspecting a target Action visually.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

from . import blender_source_adapter, workflow


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACTS_ROOT = _PLUGIN_ROOT / "retarget_artifacts" / "debug_source_comparison"


def export_source_motion_comparison(source_armature, source_action, scene, bvh_path) -> Path:
    """Save FK comparisons for direct BVH and imported-BVH source motions.

    DEBUG ONLY: the imported Action is read through the production AvaCapo
    source adapter.  The artifact validates that inverse adapter before any
    retargeting occurs.
    """
    direct_motion = workflow.load_bvh_motion(bvh_path)
    debug_motion = blender_source_adapter.motion_from_avacapo_action(
        source_armature,
        source_action,
        scene,
    )
    artifact = _build_comparison(direct_motion, debug_motion, bvh_path)

    directory = _ARTIFACTS_ROOT / _safe_path_component(source_armature.name)
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / f"{_safe_path_component(source_action.name)}.json"
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _build_comparison(direct_motion, debug_motion, bvh_path) -> dict:
    import broom.kinematics

    direct_rest_positions, direct_rest_rotations = (
        broom.kinematics.compute_global_transforms_from_local(direct_motion.hierarchy)
    )
    debug_rest_positions, debug_rest_rotations = (
        broom.kinematics.compute_global_transforms_from_local(debug_motion.hierarchy)
    )
    direct_positions, direct_rotations = broom.kinematics.compute_global_transforms(direct_motion)
    debug_positions, debug_rotations = broom.kinematics.compute_global_transforms(debug_motion)

    direct_names = tuple(direct_motion.hierarchy.joint_names)
    debug_names = tuple(debug_motion.hierarchy.joint_names)
    common_names = tuple(name for name in direct_names if name in set(debug_names))
    if not common_names:
        raise ValueError("Direct BVH and debug source Action have no joint names in common.")

    common_frame_count = min(direct_motion.frame_count, debug_motion.frame_count)
    if common_frame_count < 1:
        raise ValueError("Both source motions must contain at least one frame.")
    sample_frames = tuple(sorted({0, common_frame_count // 2, common_frame_count - 1}))

    direct_index = {name: index for index, name in enumerate(direct_names)}
    debug_index = {name: index for index, name in enumerate(debug_names)}
    rest_rows = _joint_rows(
        common_names,
        direct_index,
        debug_index,
        direct_rest_positions[0],
        direct_rest_rotations[0],
        debug_rest_positions[0],
        debug_rest_rotations[0],
    )
    motion_rows = {
        str(frame): _joint_rows(
            common_names,
            direct_index,
            debug_index,
            direct_positions[frame],
            direct_rotations[frame],
            debug_positions[frame],
            debug_rotations[frame],
        )
        for frame in sample_frames
    }
    return {
        "purpose": "DEBUG ONLY: direct BVH Motion versus imported Blender Action Motion before retargeting.",
        "coordinate_contract": (
            "Both sides are canonical AvaCapo source coordinates. The imported "
            "Action has been inverted through its Blender rest bases and source G."
        ),
        "direct_bvh_path": str(Path(bvh_path)),
        "direct": _motion_metadata(direct_motion),
        "debug": _motion_metadata(debug_motion),
        "common_joint_names": common_names,
        "direct_only_joint_names": tuple(name for name in direct_names if name not in debug_index),
        "debug_only_joint_names": tuple(name for name in debug_names if name not in direct_index),
        "sample_frame_indices": sample_frames,
        "rest_pose": rest_rows,
        "motion_samples": motion_rows,
        "summary": _summary(rest_rows, motion_rows),
    }


def _motion_metadata(motion) -> dict:
    return {
        "frame_count": int(motion.frame_count),
        "frame_time": float(motion.frame_time),
        "joint_names": tuple(motion.hierarchy.joint_names),
        "total_channels": int(motion.hierarchy.total_channels),
    }


def _joint_rows(names, direct_index, debug_index, direct_positions, direct_rotations, debug_positions, debug_rotations):
    rows = {}
    for name in names:
        source_index = direct_index[name]
        debug_index_value = debug_index[name]
        direct_position = direct_positions[source_index]
        debug_position = debug_positions[debug_index_value]
        direct_rotation = direct_rotations[source_index]
        debug_rotation = debug_rotations[debug_index_value]
        rows[name] = {
            "direct_position": _vector(direct_position),
            "debug_position": _vector(debug_position),
            "position_error": float(np.linalg.norm(debug_position - direct_position)),
            "direct_rotation": _matrix(direct_rotation),
            "debug_rotation": _matrix(debug_rotation),
            "rotation_error_degrees": _rotation_angle_degrees(direct_rotation, debug_rotation),
            "debug_from_direct_rotation": _matrix(debug_rotation @ direct_rotation.T),
        }
    return rows


def _summary(rest_rows, motion_rows) -> dict:
    all_rows = [*rest_rows.values()]
    for rows in motion_rows.values():
        all_rows.extend(rows.values())
    return {
        "max_position_error": max(float(row["position_error"]) for row in all_rows),
        "max_rotation_error_degrees": max(
            float(row["rotation_error_degrees"]) for row in all_rows
        ),
    }


def _rotation_angle_degrees(first, second) -> float:
    relative = np.asarray(second) @ np.asarray(first).T
    cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _vector(value) -> list[float]:
    return [float(component) for component in value]


def _matrix(value) -> list[list[float]]:
    return [[float(component) for component in row] for row in value]


def _safe_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "unnamed"
