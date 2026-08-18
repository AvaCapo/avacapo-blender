"""Pose-constraint helpers and in-memory NPZ payload storage."""

from __future__ import annotations

from collections.abc import Iterable

import bpy
import numpy as np
from mathutils import Matrix

from . import bvh_smpl
from .config import Config
from .retarget_maps import SMPLX_TO_SMPL_BVH, canonical_bone_name
from .rig_utils import infer_rig_type


CONSTRAINT_TYPE_ITEMS = (
    ("fullbody", "Full Body", "Constrain the complete body pose"),
    ("end-effector", "End Effector", "Constrain one selected end effector"),
    ("left-hand", "Left Hand", "Constrain the left hand"),
    ("right-hand", "Right Hand", "Constrain the right hand"),
    ("left-foot", "Left Foot", "Constrain the left foot"),
    ("right-foot", "Right Foot", "Constrain the right foot"),
)

END_EFFECTOR_ITEMS = (
    ("LeftFoot", "Left Foot", "Use the left foot as an end effector", 1),
    ("RightFoot", "Right Foot", "Use the right foot as an end effector", 2),
    ("LeftHand", "Left Hand", "Use the left hand as an end effector", 4),
    ("RightHand", "Right Hand", "Use the right hand as an end effector", 8),
    ("Hips", "Hips", "Use the hips as an end effector", 16),
)

CONSTRAINT_TYPES = frozenset(item[0] for item in CONSTRAINT_TYPE_ITEMS)
END_EFFECTOR_NAMES = frozenset(item[0] for item in END_EFFECTOR_ITEMS)
END_EFFECTOR_ORDER = tuple(item[0] for item in END_EFFECTOR_ITEMS)

# The generated BVH uses LeftToe/RightToe while Mixamo normally uses *ToeBase.
_BONE_ALIASES = {
    "LeftToe": ("LeftToe", "LeftToeBase"),
    "RightToe": ("RightToe", "RightToeBase"),
}

_constraint_payloads: dict[str, bytes] = {}


def selected_joint_names(value: Iterable[str] | str | None) -> list[str]:
    """Return selected end effectors as a stable, API-ready list."""

    if isinstance(value, str):
        selected = {value} if value else set()
    else:
        selected = set(value or ())
    return [name for name in END_EFFECTOR_ORDER if name in selected]


def cache_constraint_payload(key: str, payload: bytes) -> None:
    _constraint_payloads[key] = bytes(payload)


def get_constraint_payload(key: str) -> bytes | None:
    return _constraint_payloads.get(key)


def remove_constraint_payload(key: str) -> None:
    _constraint_payloads.pop(key, None)


def clear_constraint_payloads() -> None:
    _constraint_payloads.clear()


def armature_poll(_self, obj: bpy.types.Object | None) -> bool:
    return obj is not None and obj.type == "ARMATURE"


def source_armature(context: bpy.types.Context, settings) -> bpy.types.Object | None:
    configured = settings.constraint_source_armature
    if configured is not None:
        return configured
    obj = context.object
    return obj if obj is not None and obj.type == "ARMATURE" else None


def source_timeline_frames(scene: bpy.types.Scene, source_mode: str) -> list[int]:
    if source_mode == "CURRENT_POSE":
        return [int(scene.frame_current)]
    if source_mode != "PREVIEW_RANGE":
        raise ValueError(f"Unknown pose source mode: {source_mode}")
    if not scene.use_preview_range:
        raise ValueError("Enable a Timeline Preview Range for an animation constraint")

    start = int(scene.frame_preview_start)
    end = int(scene.frame_preview_end)
    if end < start:
        raise ValueError("Timeline Preview Range is empty")
    return list(range(start, end + 1))


def source_frame_count(scene: bpy.types.Scene, source_mode: str) -> int:
    if source_mode == "CURRENT_POSE":
        return 1
    if not scene.use_preview_range:
        return 0
    return max(0, int(scene.frame_preview_end) - int(scene.frame_preview_start) + 1)


def output_frame_count(settings) -> int:
    return max(1, int(round(settings.end - settings.start)))


def validate_constraint_settings(context: bpy.types.Context, settings) -> str | None:
    num_frames = output_frame_count(settings)
    if settings.constraint_type not in CONSTRAINT_TYPES:
        return "Invalid constraint type"
    if settings.constraint_type == "end-effector":
        joint_names = set(settings.constraint_joint_name)
        if not joint_names or not joint_names.issubset(END_EFFECTOR_NAMES):
            return "Select at least one end-effector joint"

    if settings.constraint_input == "DIRECTION":
        direction = np.asarray(settings.constraint_direction, dtype=np.float64)
        if direction.shape != (3,) or not np.isfinite(direction).all():
            return "Direction must contain three finite values"
        if float(np.linalg.norm(direction)) <= 1.0e-8:
            return "Direction cannot be zero"
        return None

    armature = source_armature(context, settings)
    if armature is None:
        return "Select a source armature"
    if infer_rig_type(armature) == "unknown":
        return "Constraint source must use an AvaCapo or Mixamo rig"

    try:
        source_frames = source_timeline_frames(context.scene, settings.constraint_pose_source)
    except ValueError as exc:
        return str(exc)

    source_index = 0 if len(source_frames) == 1 else int(settings.constraint_source_frame)
    if not 0 <= source_index < len(source_frames):
        return f"Source Frame must be between 0 and {len(source_frames) - 1}"

    target_frame = int(settings.constraint_target_frame)
    if not 0 <= target_frame < num_frames:
        return f"Target Frame must be between 0 and {num_frames - 1}"
    return None


def validate_saved_constraints(settings) -> str | None:
    """Validate constraints already captured for the next generation request."""

    num_frames = output_frame_count(settings)
    direction_count = 0

    for index, constraint in enumerate(settings.constraints, start=1):
        label = f"Constraint {index}"
        if constraint.constraint_type not in CONSTRAINT_TYPES:
            return f"{label}: invalid constraint type"
        if constraint.constraint_type == "end-effector":
            joint_names = set(constraint.constraint_joint_name)
            if not joint_names or not joint_names.issubset(END_EFFECTOR_NAMES):
                return f"{label}: select at least one end-effector joint"

        if constraint.constraint_input == "DIRECTION":
            direction_count += 1
            if direction_count > 1:
                return "Only one direction constraint can be added"
            direction = np.asarray(constraint.direction, dtype=np.float64)
            if direction.shape != (3,) or not np.isfinite(direction).all():
                return f"{label}: direction must contain three finite values"
            if float(np.linalg.norm(direction)) <= 1.0e-8:
                return f"{label}: direction cannot be zero"
            continue

        if constraint.constraint_input != "POSE":
            return f"{label}: invalid input type"
        if constraint.source_frame_count <= 0:
            return f"{label}: pose source is empty"
        if not 0 <= constraint.source_frame < constraint.source_frame_count:
            return (
                f"{label}: Source Frame must be between 0 and "
                f"{constraint.source_frame_count - 1}"
            )
        if not 0 <= constraint.target_frame < num_frames:
            return f"{label}: Target Frame must be between 0 and {num_frames - 1}"
        if get_constraint_payload(constraint.uid) is None:
            return f"{label}: captured pose is no longer in memory"

    return None


def _pose_bone_lookup(armature: bpy.types.Object) -> dict[str, bpy.types.PoseBone]:
    return {canonical_bone_name(pose_bone.name): pose_bone for pose_bone in armature.pose.bones}


def _find_pose_bone(
    lookup: dict[str, bpy.types.PoseBone], source_name: str
) -> bpy.types.PoseBone | None:
    for candidate in _BONE_ALIASES.get(source_name, (source_name,)):
        pose_bone = lookup.get(canonical_bone_name(candidate))
        if pose_bone is not None:
            return pose_bone
    return None


def _axis_angle(rotation: Matrix) -> np.ndarray:
    quaternion = rotation.to_quaternion().normalized()
    if quaternion.w < 0.0:
        quaternion = type(quaternion)((-quaternion.w, -quaternion.x, -quaternion.y, -quaternion.z))
    axis, angle = quaternion.to_axis_angle()
    return np.asarray(tuple(axis), dtype=np.float64) * float(angle)


def _sample_pose(
    armature: bpy.types.Object,
    pose_bones: Iterable[bpy.types.PoseBone],
) -> tuple[np.ndarray, np.ndarray]:
    local_rotations: list[Matrix] = []
    root_translation = None

    for index, pose_bone in enumerate(pose_bones):
        try:
            basis = armature.convert_space(
                pose_bone=pose_bone,
                matrix=pose_bone.matrix,
                from_space="POSE",
                to_space="LOCAL",
            )
        except (RuntimeError, TypeError, ValueError):
            # ``matrix_basis`` is the same space for unconstrained bones and is
            # available as a fallback for rigs Blender cannot convert directly.
            basis = pose_bone.matrix_basis.copy()

        rest_rotation = pose_bone.bone.matrix_local.to_quaternion().to_matrix()
        source_rotation = (
            rest_rotation @ basis.to_quaternion().to_matrix() @ rest_rotation.transposed()
        )
        local_rotations.append(source_rotation)

        if index == 0:
            root_translation = rest_rotation @ basis.to_translation()

    if root_translation is None:
        raise ValueError("The constraint rig has no root bone")

    if not Config.KEEP_Y_UP:
        coordinate_transform = Matrix(tuple(map(tuple, bvh_smpl.Y_UP_TO_AMASS_Z_UP)))
        local_rotations[0] = coordinate_transform @ local_rotations[0]
        root_translation = coordinate_transform @ root_translation

    body_pose = np.concatenate([_axis_angle(rotation) for rotation in local_rotations])
    if body_pose.shape != (66,):
        raise ValueError(f"Unexpected SMPL-X body pose shape: {body_pose.shape}")
    return body_pose, np.asarray(tuple(root_translation), dtype=np.float32)


def create_pose_constraint_npz(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    source_mode: str,
) -> tuple[bytes, int]:
    """Sample an evaluated Blender pose/range and return an in-memory SMPL-X NPZ."""

    if armature.type != "ARMATURE" or armature.pose is None:
        raise ValueError("Constraint source is not an armature")
    if infer_rig_type(armature) == "unknown":
        raise ValueError("Constraint source must use an AvaCapo or Mixamo rig")

    lookup = _pose_bone_lookup(armature)
    pose_bones = [
        _find_pose_bone(lookup, source_name)
        for _target_name, source_name, _parent in SMPLX_TO_SMPL_BVH
    ]
    missing = [
        source_name
        for pose_bone, (_target_name, source_name, _parent) in zip(pose_bones, SMPLX_TO_SMPL_BVH)
        if pose_bone is None
    ]
    if missing:
        raise ValueError("Constraint rig is missing bones: " + ", ".join(missing))
    resolved_pose_bones = [pose_bone for pose_bone in pose_bones if pose_bone is not None]

    frames = source_timeline_frames(context.scene, source_mode)
    previous_frame = int(context.scene.frame_current)
    poses: list[np.ndarray] = []
    translations: list[np.ndarray] = []

    if source_mode == "CURRENT_POSE":
        # Do not call ``frame_set`` here: re-evaluating the same frame can
        # discard an artist's unkeyed pose edits.
        context.view_layer.update()
        pose, translation = _sample_pose(armature, resolved_pose_bones)
        poses.append(pose)
        translations.append(translation)
    else:
        try:
            for frame in frames:
                context.scene.frame_set(frame)
                context.view_layer.update()
                pose, translation = _sample_pose(armature, resolved_pose_bones)
                poses.append(pose)
                translations.append(translation)
        finally:
            context.scene.frame_set(previous_frame)
            context.view_layer.update()

    frame_count = len(frames)
    body_poses = np.stack(poses, axis=0)
    result = {
        "poses": np.concatenate(
            (body_poses, np.zeros((frame_count, 99), dtype=np.float64)), axis=1
        ),
        "trans": np.stack(translations, axis=0).astype(np.float32, copy=False),
        "gender": np.asarray(Config.GENDER),
        "mocap_framerate": np.asarray(float(context.scene.render.fps), dtype=np.float64),
        "betas": np.zeros(16, dtype=np.float32),
    }
    return bvh_smpl.create_smpl_npz_bytes(result), frame_count
