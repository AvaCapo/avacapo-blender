"""Pose-constraint helpers and in-memory NPZ payload storage."""

from __future__ import annotations

from collections.abc import Iterable

import bpy
import numpy as np
from mathutils import Vector

from . import bvh_npz
from .retargeting import blender_pose_adapter
from .retarget_maps import NPZ_BVH_MAP


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
    try:
        _resolved_pose_bone_names(armature)
    except ValueError as exc:
        return str(exc)

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


def create_object_target_constraint_npz(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    target_object: bpy.types.Object,
    joint_name: str,
    target_offset: Iterable[float] = (0.0, 0.0, 0.0),
) -> tuple[bytes, int]:
    """Solve a temporary Blender IK pose toward an object and serialize it.

    The source armature is never keyed or permanently modified. A temporary IK
    constraint is evaluated, sampled through Broom's body-model adapter, and then
    removed even if conversion fails.
    """

    _resolved_pose_bone_names(armature)
    if target_object is None:
        raise ValueError("Select a target object or Empty")
    if target_object == armature:
        raise ValueError("Target object must be different from the source armature")
    if joint_name not in _OBJECT_TARGET_IK_CHAINS:
        raise ValueError(f"Unsupported object-target end effector: {joint_name}")

    offset = Vector(tuple(float(value) for value in target_offset))
    if len(offset) != 3 or not all(np.isfinite(value) for value in offset):
        raise ValueError("Target Offset must contain three finite values")

    owner_name, chain_count = _OBJECT_TARGET_IK_CHAINS[joint_name]
    owner_bone = armature.pose.bones[owner_name]

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


def _resolved_pose_bone_names(
    armature: bpy.types.Object,
    source_to_bone: dict[str, str] | None = None,
) -> dict[str, str]:
    """Require mapped body bones and their ancestor relationships."""
    if armature.type != "ARMATURE" or armature.pose is None:
        raise ValueError("Constraint source is not an armature")
    bones = armature.pose.bones
    resolved = {
        source: source if source_to_bone is None else source_to_bone.get(source, '')
        for _, source, _ in NPZ_BVH_MAP
    }
    missing = [
        source for source, name in resolved.items() if not name or bones.get(name) is None
    ]
    if missing:
        raise ValueError(
            ("Constraint source must use an AvaCapo rig; missing bones: "
             if source_to_bone is None else "Constraint source is missing bones: ")
            + ", ".join(missing)
        )
    if len(set(resolved.values())) != len(resolved):
        raise ValueError("Constraint body joints must map to different bones")
    for _, source_name, parent_index in NPZ_BVH_MAP:
        if parent_index < 0:
            continue
        expected_parent = resolved[NPZ_BVH_MAP[parent_index][1]]
        ancestor = bones[resolved[source_name]].parent
        while ancestor is not None and ancestor.name != expected_parent:
            ancestor = ancestor.parent
        if ancestor is None:
            raise ValueError(
                f"Constraint source: {resolved[source_name]} must descend from {expected_parent}"
            )
    return resolved


def _serialize_pose_samples(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    source_to_bone: dict[str, str],
    frames: Iterable[int],
    *,
    use_current_evaluation: bool = False,
) -> tuple[bytes, int]:
    motion, rotation_offsets = blender_pose_adapter.sample_pose_motion(
        context,
        armature,
        source_to_bone,
        frames,
        use_current_evaluation=use_current_evaluation,
    )
    result = bvh_npz.convert_motion_npz(
        motion,
        body_to_source=tuple(
            (target, source_to_bone[source], parent)
            for target, source, parent in NPZ_BVH_MAP
        ),
        source_global_rotation_offsets=rotation_offsets,
    )
    return bvh_npz.create_npz_bytes(result), motion.frame_count


def create_pose_constraint_npz_at_frames(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    frames: Iterable[int],
    *,
    source_to_bone: dict[str, str] | None = None,
) -> tuple[bytes, int]:
    """Sample explicit evaluated timeline frames into an in-memory SMPL-X NPZ."""

    source_to_bone = _resolved_pose_bone_names(armature, source_to_bone)
    return _serialize_pose_samples(
        context,
        armature,
        source_to_bone,
        frames,
    )


def create_pose_constraint_npz(
    context: bpy.types.Context,
    armature: bpy.types.Object,
    source_mode: str,
) -> tuple[bytes, int]:
    """Sample an evaluated Blender pose/range and return an in-memory SMPL-X NPZ."""

    source_to_bone = _resolved_pose_bone_names(armature)
    frames = source_timeline_frames(context.scene, source_mode)
    return _serialize_pose_samples(
        context,
        armature,
        source_to_bone,
        frames,
        use_current_evaluation=source_mode == "CURRENT_POSE",
    )
