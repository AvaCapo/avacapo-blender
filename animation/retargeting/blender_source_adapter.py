"""Blender adapter for AvaCapo source Actions.

This is the inverse boundary for source Actions imported from AvaCapo's
canonical BVH contract. It is intentionally *not* a generic Blender Action
exporter: a generic Action has no portable declaration of the source rest
frame or channel contract needed to recover a portable ``Motion`` safely.
"""

from __future__ import annotations

import math

import numpy as np
from mathutils import Euler, Matrix

from . import blender_adapter, canonical_source


def motion_from_avacapo_action(source_armature, action, scene):
    """Recover canonical ``Motion`` from an AvaCapo source Action.

    The returned hierarchy deliberately has identity ``local_orientation``.
    Blender's bone rest bases are an implementation detail of the imported
    armature, whereas the AvaCapo BVH source contract expresses its channels
    in the canonical source frame.  ``G_source`` and each bone rest basis are
    inverted while sampling the Action.
    """
    _require_canonical_source_armature(source_armature)
    if action is None:
        raise ValueError("Choose an AvaCapo source Action.")

    source_to_bone = _canonical_source_to_bone_map(source_armature)
    source_from_armature = blender_adapter.canonical_from_armature_mapping(
        source_armature,
        source_to_bone,
    )
    hierarchy = _canonical_hierarchy_from_armature(source_armature, source_from_armature)
    frame_start, frame_end = _integer_action_range(action)
    animation_data = source_armature.animation_data_create()
    previous_action = animation_data.action
    previous_slot = getattr(animation_data, "action_slot", None)
    previous_frame = int(scene.frame_current)
    action_slot = _action_slot_for_armature(action, source_armature)

    try:
        animation_data.action = action
        animation_data.action_slot = action_slot
        values = _sample_action_frames(
            source_armature,
            hierarchy,
            source_from_armature,
            scene,
            frame_start,
            frame_end,
        )
    finally:
        animation_data.action = previous_action
        if previous_slot is not None:
            animation_data.action_slot = previous_slot
        scene.frame_set(previous_frame)

    import broom

    return broom.Motion(
        hierarchy,
        np.asarray(values, dtype=np.float64),
        _action_frame_time(action),
    )


def _canonical_hierarchy_from_armature(source_armature, source_from_armature):
    """Build the portable source hierarchy, not Blender's oriented bone rig."""
    import broom

    source_from_armature_matrix = Matrix(source_from_armature.tolist())
    bones = source_armature.data.bones
    joints = []
    for source_name, parent_index in canonical_source.CANONICAL_SOURCE_TOPOLOGY:
        bone = bones[source_name]
        parent_name = canonical_source.parent_name(parent_index)
        if parent_name is None:
            offset = source_from_armature_matrix @ bone.head_local
            channels = (
                "Xposition", "Yposition", "Zposition",
                "Xrotation", "Yrotation", "Zrotation",
            )
        else:
            parent_head = bones[parent_name].head_local
            offset = source_from_armature_matrix @ (bone.head_local - parent_head)
            channels = ("Xrotation", "Yrotation", "Zrotation")
        joints.append(broom.Joint(
            name=source_name,
            parent=parent_index,
            offset=tuple(float(component) for component in offset),
            channels=channels,
            local_orientation=(1.0, 0.0, 0.0, 0.0),
        ))
    return broom.Hierarchy(tuple(joints))


def _sample_action_frames(
    source_armature,
    hierarchy,
    source_from_armature,
    scene,
    frame_start,
    frame_end,
):
    """Invert Blender bone-basis animation into canonical BVH channels."""
    source_from_armature_matrix = Matrix(source_from_armature.tolist())
    armature_from_source_matrix = source_from_armature_matrix.transposed()
    pose_bones = source_armature.pose.bones
    data_bones = source_armature.data.bones
    previous_eulers = [Euler((0.0, 0.0, 0.0), "ZYX") for _joint in hierarchy.joints]
    samples = []
    for frame in range(frame_start, frame_end + 1):
        scene.frame_set(frame)
        row = []
        for joint_index, joint in enumerate(hierarchy.joints):
            pose_bone = pose_bones[joint.name]
            rest_basis = data_bones[joint.name].matrix_local.to_3x3()
            rest_basis_inverse = rest_basis.inverted()
            basis = pose_bone.matrix_basis.copy()
            if joint.parent == -1:
                # Forward import writes location = B^-1 (p - head).  Recover
                # p in armature-data space, then map it into canonical source.
                armature_position = rest_basis @ basis.to_translation() + data_bones[joint.name].head_local
                source_position = source_from_armature_matrix @ armature_position
                row.extend(float(source_position[axis]) for axis in range(3))

            # Forward import writes P = B^-1 (G^-1 R G) B. Therefore
            # R = G B P B^-1 G^-1.
            source_rotation = (
                source_from_armature_matrix
                @ rest_basis
                @ basis.to_3x3()
                @ rest_basis_inverse
                @ armature_from_source_matrix
            )
            euler = source_rotation.to_euler("ZYX", previous_eulers[joint_index])
            previous_eulers[joint_index] = euler
            row.extend(math.degrees(float(euler[axis])) for axis in range(3))
        samples.append(row)
    return samples


def _canonical_source_to_bone_map(source_armature) -> dict[str, str]:
    _require_canonical_source_armature(source_armature)
    return {
        source_name: source_name
        for source_name, _parent in canonical_source.CANONICAL_SOURCE_TOPOLOGY
    }


def _require_canonical_source_armature(armature) -> None:
    if armature is None or armature.type != "ARMATURE" or armature.pose is None:
        raise ValueError("Choose an imported AvaCapo source armature.")
    bones = armature.data.bones
    missing = [
        name
        for name, _parent in canonical_source.CANONICAL_SOURCE_TOPOLOGY
        if bones.get(name) is None
    ]
    if missing:
        raise ValueError(
            "Source armature is not the AvaCapo canonical skeleton; missing bones: "
            + ", ".join(missing)
        )
    for name, parent_index in canonical_source.CANONICAL_SOURCE_TOPOLOGY:
        expected_parent = canonical_source.parent_name(parent_index)
        actual_parent = bones[name].parent.name if bones[name].parent else None
        if actual_parent != expected_parent:
            raise ValueError(
                f"Source bone {name!r} has parent {actual_parent!r}; "
                f"expected {expected_parent!r} for the AvaCapo canonical skeleton."
            )


def _integer_action_range(action) -> tuple[int, int]:
    start, end = (float(value) for value in action.frame_range)
    first = math.ceil(start)
    last = math.floor(end)
    if last < first:
        raise ValueError("Source Action does not contain an integer animation frame.")
    return first, last


def _action_frame_time(action) -> float:
    fps = float(action.get(
        canonical_source.SOURCE_ACTION_FPS_PROPERTY,
        canonical_source.DEFAULT_SOURCE_ACTION_FPS,
    ))
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError(
            f"Action property {canonical_source.SOURCE_ACTION_FPS_PROPERTY!r} "
            "must be a positive number."
        )
    return 1.0 / fps


def _action_slot_for_armature(action, armature):
    expected_identifier = f"OB{armature.name}"
    slots = list(action.slots)
    try:
        return action.slots[expected_identifier]
    except KeyError:
        pass
    if len(slots) == 1:
        return slots[0]
    raise ValueError(
        f"Action {action.name!r} has no slot for source armature {armature.name!r}."
    )
