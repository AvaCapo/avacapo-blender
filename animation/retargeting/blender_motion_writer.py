"""Write a retargeted ``Motion`` to one Blender Action."""

from __future__ import annotations

from dataclasses import dataclass
import math

import bpy
import bpy.utils
import bpy_extras.anim_utils
from mathutils import Euler, Matrix, Vector

@dataclass(frozen=True)
class _PreparedCurve:
    data_path: str
    group_name: str
    samples: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class _PreparedMotion:
    frame_times: tuple[float, ...]
    curves: tuple[_PreparedCurve, ...]


@dataclass(frozen=True)
class _CurveSnapshot:
    data_path: str
    array_index: int
    group_name: str | None
    extrapolation: str
    points: tuple[tuple[float, float, str], ...]


def prepare_retargeted_motion(
    skeleton,
    motion,
    *,
    canonical_from_armature,
    frame_start: float = 1.0,
    frame_step: float = 1.0,
) -> _PreparedMotion:
    """Validate and calculate every Blender curve without mutating an Action."""
    if skeleton.type != "ARMATURE" or skeleton.pose is None:
        raise ValueError("Retarget target must be an armature with a pose.")
    if motion.frame_count < 1:
        raise ValueError("Retargeted motion must contain at least one frame.")
    if not math.isfinite(frame_start) or not math.isfinite(frame_step) or frame_step <= 0.0:
        raise ValueError("Frame start and frame step must be finite; frame step must be positive.")

    hierarchy = motion.hierarchy
    bases = _current_retarget_bases(skeleton)
    missing_pose_bones = [name for name in hierarchy.joint_names if name not in skeleton.pose.bones]
    if missing_pose_bones:
        raise ValueError("Target armature is missing motion bones: " + ", ".join(missing_pose_bones))
    missing_bases = [name for name in hierarchy.joint_names if name not in bases]
    if missing_bases:
        raise ValueError("Confirmed rest basis is missing motion bones: " + ", ".join(missing_bases))

    frame_times = tuple(float(frame_start + index * frame_step) for index in range(motion.frame_count))
    armature_from_canonical = Matrix(canonical_from_armature).inverted()
    armature_from_canonical.resize_4x4()
    curves = []
    for joint_index, joint in enumerate(hierarchy.joints):
        pose_bone = skeleton.pose.bones[joint.name]
        channel_start = hierarchy.channel_start(joint_index)
        position_columns = {
            channel[0]: channel_start + offset
            for offset, channel in enumerate(joint.channels)
            if channel.endswith("position")
        }
        if position_columns:
            locations = _retarget_joint_locations(
                motion, joint_index, position_columns, armature_from_canonical, bases
            )
            curves.append(_PreparedCurve(
                data_path=f'pose.bones["{bpy.utils.escape_identifier(joint.name)}"].location',
                group_name=joint.name,
                samples=tuple(locations),
            )
            )

        rotation_columns = {
            channel[0]: channel_start + offset
            for offset, channel in enumerate(joint.channels)
            if channel.endswith("rotation")
        }
        if rotation_columns:
            samples, data_path = _rotation_samples(
                motion, joint_index, rotation_columns, pose_bone.rotation_mode
            )
            curves.append(_PreparedCurve(
                data_path=data_path,
                group_name=joint.name,
                samples=tuple(samples),
            ))
    return _PreparedMotion(frame_times=frame_times, curves=tuple(curves))


def apply_prepared_motion(skeleton, action, prepared, *, assign_to_skeleton: bool) -> None:
    """Write already validated curves to one Action.

    Callers that replace an existing Action should use a staging Action first,
    then :func:`replace_action_slot_from_staging` below.
    """
    action_slot = action_slot_for_object(action, skeleton)
    channelbag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(action, action_slot)
    for fcurve in list(channelbag.fcurves):
        channelbag.fcurves.remove(fcurve)
    _write_prepared_curves(channelbag, prepared)
    if assign_to_skeleton:
        assign_action(skeleton, action, action_slot)


def replace_action_slot_from_staging(
    skeleton,
    destination_action,
    staging_action,
    *,
    finalize,
    rollback_finalize,
) -> None:
    """Commit staged curves into an existing Action, restoring on failure.

    ``finalize`` writes metadata that belongs to the same logical output
    transaction (currently retarget provenance). If it fails after curves were
    replaced, both curves and metadata are restored through
    ``rollback_finalize``.
    """
    destination_slot = action_slot_for_object(destination_action, skeleton)
    staging_slot = action_slot_for_object(staging_action, skeleton)
    destination_bag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(
        destination_action,
        destination_slot,
    )
    staging_bag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(
        staging_action,
        staging_slot,
    )
    previous = _snapshot_curves(destination_bag)
    staged = _snapshot_curves(staging_bag)
    try:
        _replace_curves(destination_bag, staged)
        finalize()
        assign_action(skeleton, destination_action, destination_slot)
    except Exception:
        _replace_curves(destination_bag, previous)
        rollback_finalize()
        raise


def assign_action(skeleton, action, action_slot) -> None:
    animation_data = skeleton.animation_data_create()
    animation_data.action = action
    animation_data.action_slot = action_slot


def _current_retarget_bases(skeleton) -> dict[str, Matrix]:
    return {bone.name: bone.matrix_local.to_3x3() for bone in skeleton.data.bones}


def _retarget_joint_locations(motion, joint_index, position_columns, armature_from_canonical, rest_bases):
    """Convert local joint translations to Blender pose-bone locations."""
    hierarchy = motion.hierarchy
    joint = hierarchy.joints[joint_index]
    joint_basis_inverse = rest_bases[joint.name].inverted()
    parent_basis = None
    if joint.parent != -1:
        parent_basis = rest_bases[hierarchy.joints[joint.parent].name]
    locations = []
    for values in motion.values:
        local_delta = Vector(tuple(
            float(values[position_columns[axis]]) if axis in position_columns else 0.0
            for axis in "XYZ"
        ))
        if parent_basis is None:
            armature_delta = (armature_from_canonical @ Matrix.Translation(local_delta)).to_translation()
        else:
            # Child translations use parent rest-local axes; Blender pose-bone
            # locations use the current bone's rest-local axes.
            armature_delta = parent_basis @ local_delta
        locations.append(tuple(joint_basis_inverse @ armature_delta))
    return locations


def _rotation_samples(motion, joint_index, rotation_columns, rotation_mode):
    joint = motion.hierarchy.joints[joint_index]
    order = "".join(channel[0] for channel in joint.channels if channel.endswith("rotation"))
    if len(order) != 3:
        raise ValueError(f"Joint {joint.name!r} must have three rotation channels.")
    use_quaternion = rotation_mode == "QUATERNION"
    blender_order = rotation_mode if rotation_mode in {
        "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX",
    } else "XYZ"
    data_path = (
        f'pose.bones["{bpy.utils.escape_identifier(joint.name)}"].rotation_quaternion'
        if use_quaternion
        else f'pose.bones["{bpy.utils.escape_identifier(joint.name)}"].rotation_euler'
    )
    samples = []
    previous_euler = Euler((0.0, 0.0, 0.0), blender_order)
    for values in motion.values:
        angles = tuple(math.radians(float(values[rotation_columns[axis]])) for axis in "XYZ")
        channel_rotation = Euler(angles, order[::-1]).to_matrix().to_4x4()
        if use_quaternion:
            samples.append(tuple(channel_rotation.to_quaternion()))
        else:
            euler = channel_rotation.to_euler(blender_order, previous_euler)
            samples.append(tuple(euler))
            previous_euler = euler
    return samples, data_path


def _write_prepared_curves(channelbag, prepared) -> None:
    for prepared_curve in prepared.curves:
        for axis in range(len(prepared_curve.samples[0])):
            curve = channelbag.fcurves.new(
                data_path=prepared_curve.data_path,
                index=axis,
                group_name=prepared_curve.group_name,
            )
            curve.keyframe_points.add(len(prepared.frame_times))
            for index, frame in enumerate(prepared.frame_times):
                point = curve.keyframe_points[index]
                point.co = (frame, float(prepared_curve.samples[index][axis]))
                point.interpolation = "LINEAR"
            curve.update()


def _snapshot_curves(channelbag) -> tuple[_CurveSnapshot, ...]:
    return tuple(
        _CurveSnapshot(
            data_path=curve.data_path,
            array_index=int(curve.array_index),
            group_name=None if curve.group is None else curve.group.name,
            extrapolation=curve.extrapolation,
            points=tuple(
                (float(point.co[0]), float(point.co[1]), point.interpolation)
                for point in curve.keyframe_points
            ),
        )
        for curve in channelbag.fcurves
    )


def _replace_curves(channelbag, snapshots) -> None:
    for curve in list(channelbag.fcurves):
        channelbag.fcurves.remove(curve)
    for snapshot in snapshots:
        curve = channelbag.fcurves.new(
            data_path=snapshot.data_path,
            index=snapshot.array_index,
            group_name=snapshot.group_name,
        )
        curve.extrapolation = snapshot.extrapolation
        curve.keyframe_points.add(len(snapshot.points))
        for index, (frame, value, interpolation) in enumerate(snapshot.points):
            point = curve.keyframe_points[index]
            point.co = (frame, value)
            point.interpolation = interpolation
        curve.update()


def action_slot_for_object(action, obj):
    animation_data = obj.animation_data
    current_slot = None
    if animation_data is not None and animation_data.action == action:
        current_slot = getattr(animation_data, "action_slot", None)
    if current_slot is not None and any(current_slot == slot for slot in action.slots):
        return current_slot
    try:
        return action.slots[f"OB{obj.name}"]
    except (KeyError, TypeError):
        pass
    slots = list(action.slots)
    return slots[0] if len(slots) == 1 else action.slots.new(obj.id_type, name=obj.name)
