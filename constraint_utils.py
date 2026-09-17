"""Pose-constraint helpers and in-memory NPZ payload storage."""

from __future__ import annotations

from collections.abc import Iterable

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import bvh_smpl
from .config import Config
from .retarget_maps import SMPLX_TO_SMPL_BVH, canonical_bone_name
from .rig_utils import supports_smpl_input


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

OBJECT_TARGET_CONSTRAINT_TYPES = {
    "LeftHand": "left-hand",
    "RightHand": "right-hand",
    "LeftFoot": "left-foot",
    "RightFoot": "right-foot",
}
OBJECT_TARGET_ITEMS = tuple(
    item for item in END_EFFECTOR_ITEMS if item[0] in OBJECT_TARGET_CONSTRAINT_TYPES
)

# Blender's IK target is placed on the parent bone whose tail is the wrist or
# ankle represented by Kimodo's end-effector joint.
_OBJECT_TARGET_IK_CHAINS = {
    "LeftHand": ("LeftForeArm", 2),
    "RightHand": ("RightForeArm", 2),
    "LeftFoot": ("LeftLeg", 2),
    "RightFoot": ("RightLeg", 2),
}

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


def object_target_poll(_self, obj: bpy.types.Object | None) -> bool:
    return obj is not None and obj.type != "ARMATURE"


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


def object_target_joint_name(value: Iterable[str] | str | None) -> str:
    """Return the single hand/foot supported by an object-target constraint."""

    joint_names = selected_joint_names(value)
    if len(joint_names) != 1:
        raise ValueError("Object Target requires exactly one end effector")
    joint_name = joint_names[0]
    if joint_name not in OBJECT_TARGET_CONSTRAINT_TYPES:
        raise ValueError("Object Target supports hands and feet, not Hips")
    return joint_name


def object_target_constraint_type(joint_name: str) -> str:
    try:
        return OBJECT_TARGET_CONSTRAINT_TYPES[joint_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported object-target end effector: {joint_name}") from exc


def validate_constraint_settings(context: bpy.types.Context, settings) -> str | None:
    num_frames = output_frame_count(settings)
    input_type = settings.constraint_input
    if settings.constraint_type not in CONSTRAINT_TYPES:
        return "Invalid constraint type"
    if input_type == "OBJECT_TARGET":
        try:
            object_target_joint_name(settings.constraint_object_target_joint)
        except ValueError as exc:
            return str(exc)
        if settings.constraint_target_object is None:
            return "Select a target object or Empty"
    elif settings.constraint_type == "end-effector":
        joint_names = set(settings.constraint_joint_name)
        if not joint_names or not joint_names.issubset(END_EFFECTOR_NAMES):
            return "Select at least one end-effector joint"

    if input_type == "DIRECTION":
        direction = np.asarray(settings.constraint_direction, dtype=np.float64)
        if direction.shape != (3,) or not np.isfinite(direction).all():
            return "Direction must contain three finite values"
        if float(np.linalg.norm(direction)) <= 1.0e-8:
            return "Direction cannot be zero"
        return None

    armature = source_armature(context, settings)
    if armature is None:
        return "Select a source armature"
    if not supports_smpl_input(armature):
        return "Constraint source must use an AvaCapo or Mixamo rig"

    if input_type == "OBJECT_TARGET":
        if settings.constraint_target_object == armature:
            return "Target object must be different from the source armature"
        offset = np.asarray(settings.constraint_target_offset, dtype=np.float64)
        if offset.shape != (3,) or not np.isfinite(offset).all():
            return "Target Offset must contain three finite values"
        target_frame = int(settings.constraint_target_frame)
        if not 0 <= target_frame < num_frames:
            return f"Target Frame must be between 0 and {num_frames - 1}"
        return None

    if input_type != "POSE":
        return "Invalid constraint input"

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
        if constraint.constraint_input == "OBJECT_TARGET":
            try:
                object_target_joint_name(constraint.constraint_joint_name)
            except ValueError as exc:
                return f"{label}: {exc}"
        elif constraint.constraint_type == "end-effector":
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

        if constraint.constraint_input not in {"POSE", "OBJECT_TARGET"}:
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


def create_object_target_constraint_npz(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    target_object: bpy.types.Object,
    joint_name: str,
    target_offset: Iterable[float] = (0.0, 0.0, 0.0),
) -> tuple[bytes, int]:
    """Solve a temporary Blender IK pose toward an object and serialize it.

    The source armature is never keyed or permanently modified. A temporary IK
    constraint is evaluated, sampled through the existing SMPL-X path, and then
    removed even if conversion fails.
    """

    if armature.type != "ARMATURE" or armature.pose is None:
        raise ValueError("Constraint source is not an armature")
    if target_object is None:
        raise ValueError("Select a target object or Empty")
    if target_object == armature:
        raise ValueError("Target object must be different from the source armature")
    if joint_name not in _OBJECT_TARGET_IK_CHAINS:
        raise ValueError(f"Unsupported object-target end effector: {joint_name}")

    offset = Vector(tuple(float(value) for value in target_offset))
    if len(offset) != 3 or not all(np.isfinite(value) for value in offset):
        raise ValueError("Target Offset must contain three finite values")

    lookup = _pose_bone_lookup(armature)
    owner_name, chain_count = _OBJECT_TARGET_IK_CHAINS[joint_name]
    owner_bone = _find_pose_bone(lookup, owner_name)
    if owner_bone is None:
        raise ValueError(f"Constraint rig is missing IK bone: {owner_name}")

    # A target may have just been moved or created from the constraint UI.
    # Flush that transform before reading its evaluated (animated) matrix.
    context.view_layer.update()
    depsgraph = context.evaluated_depsgraph_get()
    evaluated_target = target_object.evaluated_get(depsgraph)
    target_world = evaluated_target.matrix_world @ offset

    proxy = bpy.data.objects.new("__avacapo_object_target", None)
    proxy.empty_display_type = "SPHERE"
    proxy.empty_display_size = 0.025
    proxy.location = target_world
    proxy.hide_render = True
    context.scene.collection.objects.link(proxy)

    ik_constraint = None
    try:
        ik_constraint = owner_bone.constraints.new("IK")
        ik_constraint.name = "AVACAPO_OBJECT_TARGET_IK"
        ik_constraint.target = proxy
        ik_constraint.chain_count = chain_count
        ik_constraint.use_stretch = False
        ik_constraint.iterations = 100
        if hasattr(ik_constraint, "use_tail"):
            ik_constraint.use_tail = True
        if hasattr(ik_constraint, "use_rotation"):
            ik_constraint.use_rotation = False

        context.view_layer.update()
        reached_world = armature.matrix_world @ owner_bone.tail
        miss_distance = float((reached_world - target_world).length)
        rig_size = max((float(abs(value)) for value in armature.dimensions), default=0.0)
        reach_tolerance = max(1.0e-4, rig_size * 0.01)
        print(f"Miss distance: {miss_distance:.3f}, Reach tolerance: {reach_tolerance:.3f}")
        # if miss_distance > reach_tolerance:
        #     raise ValueError(
        #         "Object target is outside the current limb reach "
        #         f"(misses by {miss_distance:.3f} Blender units). "
        #         "Move the character or target closer."
        #     )

        return create_pose_constraint_npz(context, armature, "CURRENT_POSE")
    finally:
        if ik_constraint is not None:
            owner_bone.constraints.remove(ik_constraint)
        if proxy.name in bpy.data.objects:
            bpy.data.objects.remove(proxy, do_unlink=True)
        context.view_layer.update()


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


def _resolved_smpl_pose_bones(
    armature: bpy.types.Object,
) -> list[bpy.types.PoseBone]:
    if armature.type != "ARMATURE" or armature.pose is None:
        raise ValueError("Constraint source is not an armature")
    if not supports_smpl_input(armature):
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
    return [pose_bone for pose_bone in pose_bones if pose_bone is not None]


def _serialize_pose_samples(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    resolved_pose_bones: Iterable[bpy.types.PoseBone],
    frames: Iterable[int],
    *,
    use_current_evaluation: bool = False,
) -> tuple[bytes, int]:
    sample_frames = [int(frame) for frame in frames]
    if not sample_frames:
        raise ValueError("At least one timeline frame is required")

    previous_frame = int(context.scene.frame_current)
    poses: list[np.ndarray] = []
    translations: list[np.ndarray] = []

    if use_current_evaluation:
        # Do not call ``frame_set`` here: re-evaluating the same frame can
        # discard an artist's unkeyed pose edits.
        context.view_layer.update()
        pose, translation = _sample_pose(armature, resolved_pose_bones)
        poses.append(pose)
        translations.append(translation)
    else:
        try:
            for frame in sample_frames:
                context.scene.frame_set(frame)
                context.view_layer.update()
                pose, translation = _sample_pose(armature, resolved_pose_bones)
                poses.append(pose)
                translations.append(translation)
        finally:
            context.scene.frame_set(previous_frame)
            context.view_layer.update()

    frame_count = len(sample_frames)
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


def create_pose_constraint_npz_at_frames(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    frames: Iterable[int],
) -> tuple[bytes, int]:
    """Sample explicit evaluated timeline frames into an in-memory SMPL-X NPZ."""

    resolved_pose_bones = _resolved_smpl_pose_bones(armature)
    return _serialize_pose_samples(
        context,
        armature,
        resolved_pose_bones,
        frames,
    )


def create_pose_constraint_npz(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    source_mode: str,
) -> tuple[bytes, int]:
    """Sample an evaluated Blender pose/range and return an in-memory SMPL-X NPZ."""

    resolved_pose_bones = _resolved_smpl_pose_bones(armature)
    frames = source_timeline_frames(context.scene, source_mode)
    return _serialize_pose_samples(
        context,
        armature,
        resolved_pose_bones,
        frames,
        use_current_evaluation=source_mode == "CURRENT_POSE",
    )
