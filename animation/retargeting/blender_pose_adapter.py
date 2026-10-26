"""Sample evaluated Blender poses as Broom motions without writing Actions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
import math

import numpy as np
from mathutils import Euler, Matrix, Quaternion

from broom import Hierarchy, Motion
from broom.kinematics import compute_global_transforms_from_local

from . import blender_adapter


_POSE_CHANNELS = (
    "Xposition", "Yposition", "Zposition",
    "Xrotation", "Yrotation", "Zrotation",
)


def sample_pose_motion(
    context,
    armature,
    source_to_bone: Mapping[str, str],
    frames: Iterable[int],
    *,
    use_current_evaluation: bool = False,
) -> tuple[Motion, np.ndarray]:
    """Return a canonical Y-up Motion and mapped global rest-rotation offsets.

    Offsets follow ``source_to_bone.values()`` order. All ancestors are sampled,
    including helper bones above the pelvis or between mapped body joints.
    Object placement is not baked: poses use armature-data coordinates.
    """
    sample_frames = [int(frame) for frame in frames]
    if not sample_frames:
        raise ValueError("At least one timeline frame is required")
    if use_current_evaluation and len(sample_frames) != 1:
        raise ValueError("Current evaluation requires exactly one sample")

    fps = float(context.scene.render.fps)
    fps_base = float(context.scene.render.fps_base)
    if not np.isfinite(fps) or fps <= 0.0 or not np.isfinite(fps_base) or fps_base <= 0.0:
        raise ValueError("Scene FPS and FPS Base must be finite and positive")

    source_from_armature = blender_adapter.canonical_from_armature_mapping(
        armature, source_to_bone
    )
    rest_hierarchy = blender_adapter.armature_to_hierarchy(armature, source_from_armature)
    # Store evaluated translations even for connected bones. Their joint heads
    # can move under scale, IK or inheritance settings despite use_connect.
    hierarchy = Hierarchy(tuple(
        replace(joint, channels=_POSE_CHANNELS) for joint in rest_hierarchy.joints
    ))
    _, rest_rotations = compute_global_transforms_from_local(hierarchy)
    mapped_indices = [hierarchy.joint_index(name) for name in source_to_bone.values()]
    rotation_offsets = rest_rotations[0, mapped_indices].copy()
    # O is the static orientation in R_local = O @ R_channels.
    orientations = [Quaternion(joint.local_orientation).to_matrix() for joint in hierarchy.joints]
    previous_eulers = [Euler((0.0, 0.0, 0.0), "ZYX") for _ in hierarchy.joints]

    def sample():
        context.view_layer.update()
        evaluated = armature.evaluated_get(context.evaluated_depsgraph_get())
        return _sample_evaluated_pose(
            evaluated, hierarchy, source_from_armature, orientations, previous_eulers
        )

    if use_current_evaluation:
        # frame_set, even for the current frame, discards unkeyed pose edits.
        values = [sample()]
    else:
        previous_frame = int(context.scene.frame_current)
        previous_subframe = float(context.scene.frame_subframe)
        try:
            values = []
            for frame in sample_frames:
                context.scene.frame_set(frame)
                values.append(sample())
        finally:
            context.scene.frame_set(previous_frame, subframe=previous_subframe)
            context.view_layer.update()

    return Motion(hierarchy, np.asarray(values, dtype=np.float64), fps_base / fps), rotation_offsets


def _sample_evaluated_pose(
    evaluated, hierarchy, source_from_armature, orientations, previous_eulers
) -> list[float]:
    coordinate_rotation = Matrix(source_from_armature.tolist())
    positions = []
    rotations = []
    for joint in hierarchy.joints:
        pose_bone = evaluated.pose.bones.get(joint.name)
        if pose_bone is None:
            # The rest compiler inserts an identity root for multi-root rigs.
            if joint.parent != -1 or joint.name in evaluated.data.bones:
                raise ValueError(f"Evaluated pose is missing bone: {joint.name}")
            positions.append(np.zeros(3))
            rotations.append(Matrix.Identity(3))
            continue
        matrix = pose_bone.matrix
        positions.append(source_from_armature @ np.asarray(matrix.to_translation()))
        rotations.append(coordinate_rotation @ matrix.to_quaternion().normalized().to_matrix())

    row = []
    for joint_index, joint in enumerate(hierarchy.joints):
        if joint.parent < 0:
            local_position = positions[joint_index]
            local_rotation = rotations[joint_index]
        else:
            parent_inverse = rotations[joint.parent].transposed()
            local_position = np.asarray(parent_inverse) @ (
                positions[joint_index] - positions[joint.parent]
            )
            local_rotation = parent_inverse @ rotations[joint_index]
        row.extend(float(value) for value in local_position - np.asarray(joint.offset))
        delta_rotation = orientations[joint_index].transposed() @ local_rotation
        # Broom's intrinsic XYZ channels correspond to Blender's ZYX Euler.
        euler = delta_rotation.to_euler("ZYX", previous_eulers[joint_index])
        previous_eulers[joint_index] = euler
        row.extend(math.degrees(float(value)) for value in euler)
    return row
