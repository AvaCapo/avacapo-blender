"""DEBUG ONLY: capture rest and retargeted joint positions for height diagnosis.

This module is intentionally Blender-specific.  It serializes evidence for one
target armature at a time so several rigs can be compared outside Blender
without guessing which coordinate-space conversion introduced a height shift.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACTS_ROOT = _PLUGIN_ROOT / "retarget_artifacts" / "root_height_capture"
_MAX_CAPTURE_FRAMES = 10


def export_root_height_capture(
    armature,
    action,
    scene,
    depsgraph,
    source_motion,
    source_provenance,
    *,
    current_joint_map,
    canonical_from_armature=None,
    target_hierarchy=None,
) -> Path:
    """Write rest-space and evaluated-Action evidence for one target rig.

    DEBUG ONLY.  The Action is not altered.  The scene frame is temporarily
    sampled and restored before this function returns.  The output contains
    every target rest bone in armature and world spaces, plus the first ten
    frames of source FK and evaluated target pose matrices/heads.
    """
    import broom.kinematics
    import broom.ops.skeleton_geometry

    _require_armature(armature)
    if action is None:
        raise ValueError("Choose an active retargeted Action before saving a capture.")
    if source_motion.frame_count < 1:
        raise ValueError("Retarget source Motion has no frames to capture.")

    source_rest_positions, source_rest_rotations = (
        broom.kinematics.compute_global_transforms_from_local(source_motion.hierarchy)
    )
    source_positions, source_rotations = broom.kinematics.compute_global_transforms(source_motion)
    frame_count = min(_MAX_CAPTURE_FRAMES, int(source_motion.frame_count))
    original_frame = int(scene.frame_current)
    try:
        target_frames = _capture_target_frames(
            armature,
            scene,
            depsgraph,
            frame_count,
        )
    finally:
        scene.frame_set(original_frame)

    artifact = {
        "purpose": (
            "DEBUG ONLY: compare source root trajectory, target rest geometry, "
            "and evaluated target Action placement for root-height diagnosis."
        ),
        "capture_contract": (
            "Target rest data is saved in Blender armature-data and world spaces. "
            "Target animation frames are evaluated Blender pose-bone transforms in "
            "armature object and world spaces. Source FK is in source Motion/Broom "
            "canonical clip space."
        ),
        "sample_frame_count": frame_count,
        "source": _source_capture(
            source_motion,
            source_provenance,
            source_rest_positions,
            source_rest_rotations,
            source_positions,
            source_rotations,
            frame_count,
        ),
        "target": {
            "armature_object": armature.name,
            "active_action": action.name,
            "object_matrix_world": _matrix(armature.matrix_world),
            "rest_bones": _target_rest_bones(armature),
            "current_mapping": {
                str(source): str(target)
                for source, target in sorted(current_joint_map.items())
            },
            "canonical_from_armature": (
                None if canonical_from_armature is None else _matrix(canonical_from_armature)
            ),
            "current_broom_scale_estimate": (
                None
                if target_hierarchy is None
                else float(
                    broom.ops.skeleton_geometry.estimate_skeleton_scale_ratio(
                        source_motion.hierarchy,
                        target_hierarchy,
                    )
                )
            ),
            "current_compiled_hierarchy": (
                None if target_hierarchy is None else _hierarchy_rows(target_hierarchy)
            ),
            "evaluated_action_frames": target_frames,
        },
    }
    directory = _ARTIFACTS_ROOT / _safe_path_component(armature.name)
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / f"{_safe_path_component(action.name)}.json"
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _source_capture(
    motion,
    provenance,
    rest_positions,
    rest_rotations,
    positions,
    rotations,
    frame_count,
) -> dict:
    hierarchy = motion.hierarchy
    root_name = hierarchy.root_name
    root_index = hierarchy.joint_index(root_name)
    return {
        "provenance": provenance,
        "frame_count": int(motion.frame_count),
        "frame_time": float(motion.frame_time),
        "root_joint": root_name,
        "root_offset": _vector(hierarchy.root_joint.offset),
        "rest_global_joints": _motion_joint_rows(
            hierarchy,
            rest_positions[0],
            rest_rotations[0],
        ),
        "frames": [
            {
                "source_frame_index": frame_index,
                "root_channels": _joint_channels(motion, root_index, frame_index),
                "root_global_position": _vector(positions[frame_index, root_index]),
                "global_joints": _motion_joint_rows(
                    hierarchy,
                    positions[frame_index],
                    rotations[frame_index],
                ),
            }
            for frame_index in range(frame_count)
        ],
    }


def _target_rest_bones(armature) -> dict:
    rows = {}
    for bone in armature.data.bones:
        armature_matrix = bone.matrix_local
        world_matrix = armature.matrix_world @ armature_matrix
        rows[bone.name] = {
            "parent": None if bone.parent is None else bone.parent.name,
            "use_connect": bool(bone.use_connect),
            "head_armature": _vector(bone.head_local),
            "tail_armature": _vector(bone.tail_local),
            "matrix_armature": _matrix(armature_matrix),
            "head_world": _vector(armature.matrix_world @ bone.head_local),
            "tail_world": _vector(armature.matrix_world @ bone.tail_local),
            "matrix_world": _matrix(world_matrix),
        }
    return rows


def _capture_target_frames(armature, scene, depsgraph, frame_count) -> list[dict]:
    frames = []
    for frame_number in range(1, frame_count + 1):
        scene.frame_set(frame_number)
        evaluated_armature = armature.evaluated_get(depsgraph)
        world_matrix = evaluated_armature.matrix_world
        joints = {}
        for pose_bone in evaluated_armature.pose.bones:
            armature_matrix = pose_bone.matrix
            joint_world_matrix = world_matrix @ armature_matrix
            joints[pose_bone.name] = {
                "head_armature": _vector(armature_matrix.translation),
                "matrix_armature": _matrix(armature_matrix),
                "head_world": _vector(joint_world_matrix.translation),
                "matrix_world": _matrix(joint_world_matrix),
            }
        frames.append({
            "blender_frame": frame_number,
            "joints": joints,
        })
    return frames


def _motion_joint_rows(hierarchy, positions, rotations) -> dict:
    return {
        joint.name: {
            "parent": None if joint.parent == -1 else hierarchy.joints[joint.parent].name,
            "position": _vector(positions[index]),
            "rotation": _matrix(rotations[index]),
        }
        for index, joint in enumerate(hierarchy.joints)
    }


def _hierarchy_rows(hierarchy) -> dict:
    """Return the currently compiled Broom target, without recomputing it."""
    return {
        joint.name: {
            "parent": None if joint.parent == -1 else hierarchy.joints[joint.parent].name,
            "offset": _vector(joint.offset),
            "channels": list(joint.channels),
            "local_orientation": _vector(joint.local_orientation),
            "end_site_offset": (
                None if joint.end_site_offset is None else _vector(joint.end_site_offset)
            ),
        }
        for joint in hierarchy.joints
    }


def _joint_channels(motion, joint_index, frame_index) -> dict:
    hierarchy = motion.hierarchy
    joint = hierarchy.joints[joint_index]
    start = hierarchy.channel_start(joint_index)
    return {
        channel: float(motion.values[frame_index, start + offset])
        for offset, channel in enumerate(joint.channels)
    }


def _require_armature(armature) -> None:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Choose a target armature before saving a capture.")


def _vector(value) -> list[float]:
    return [float(component) for component in value]


def _matrix(value) -> list[list[float]]:
    return [
        [float(value[row][column]) for column in range(len(value[row]))]
        for row in range(len(value))
    ]


def _safe_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "unnamed"
