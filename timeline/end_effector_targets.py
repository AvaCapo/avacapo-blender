"""Author and sample clip targets without modifying the character pose."""

from dataclasses import dataclass
import math

import bpy
from mathutils import Matrix, Vector

from ..animation.retargeting import blender_retarget_service


END_EFFECTOR_ITEMS = (
    ("LeftHand", "Left Hand", "Target the left wrist"),
    ("RightHand", "Right Hand", "Target the right wrist"),
    ("LeftFoot", "Left Foot", "Target the left ankle"),
    ("RightFoot", "Right Foot", "Target the right ankle"),
)
END_EFFECTOR_NAMES = frozenset(item[0] for item in END_EFFECTOR_ITEMS)


@dataclass(frozen=True)
class TargetSample:
    """Immutable world-space snapshot; rotations use Blender's WXYZ order."""

    joint_name: str
    target_frame: int
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]


def target_object_poll(_self, obj):
    return obj is not None and obj.type == 'EMPTY'


def validate_targets(clip, *, fullbody_frames=()):
    """Validate enabled goals before sampling or submitting a request."""
    seen = set()
    fullbody_frames = set(fullbody_frames)
    for index, target in enumerate(clip.end_effector_targets, start=1):
        if not target.enabled:
            continue
        label = f"End effector target {index}"
        if target.joint_name not in END_EFFECTOR_NAMES:
            raise ValueError(f"{label}: choose a hand or foot.")
        if target.target_object is None or target.target_object.type != 'EMPTY':
            raise ValueError(f"{label}: select a target Empty.")
        if target.target_object.name not in clip.id_data.objects:
            raise ValueError(f"{label}: target Empty must belong to this Scene.")
        if not 0 <= target.target_frame < clip.end - clip.start:
            raise ValueError(f"{label}: frame must be inside the Clip.")
        key = (target.joint_name, target.target_frame)
        if key in seen:
            raise ValueError(f"{label}: this joint already has a target on this frame.")
        if target.target_frame in fullbody_frames:
            raise ValueError(f"{label}: this frame already has a full-body pose constraint.")
        seen.add(key)


def create_target(context, track, clip, joint_name):
    """Place an Empty at the evaluated mapped joint on the current clip frame."""
    if joint_name not in END_EFFECTOR_NAMES:
        raise ValueError("Choose a hand or foot.")
    frame = int(context.scene.frame_current)
    if not clip.start <= frame < clip.end:
        raise ValueError("Move the current frame inside the Clip before adding a target.")
    if any(target.enabled and target.joint_name == joint_name
           and target.target_frame == frame - clip.start
           for target in clip.end_effector_targets):
        raise ValueError("This joint already has a target on the current frame.")
    armature = track.object
    mapping = blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping)
    bone_name = mapping.get(joint_name)
    if not bone_name or armature.pose.bones.get(bone_name) is None:
        raise ValueError(f"Map {joint_name} in the retargeting settings first.")
    context.view_layer.update()
    evaluated = armature.evaluated_get(context.evaluated_depsgraph_get())
    joint_world = evaluated.matrix_world @ evaluated.pose.bones[bone_name].matrix
    # An Empty represents a rigid target, independent of bone/object scale.
    location, rotation, _scale = joint_world.decompose()
    obj = bpy.data.objects.new(f"{clip.name} {joint_name} Target", None)
    try:
        context.scene.collection.objects.link(obj)
        obj.empty_display_type = 'ARROWS'
        obj.empty_display_size = 0.15
        obj.rotation_mode = 'QUATERNION'
        obj.matrix_world = Matrix.LocRotScale(location, rotation, None)
        target = clip.end_effector_targets.add()
        target.joint_name = joint_name
        target.target_frame = frame - clip.start
        target.target_object = obj
    except Exception:
        if 'target' in locals():
            clip.end_effector_targets.remove(len(clip.end_effector_targets) - 1)
        bpy.data.objects.remove(obj, do_unlink=True)
        raise
    return target


def sample_targets(context, clip):
    """Sample animated targets at scene frames, restoring the user's playhead."""
    validate_targets(clip)
    scene = context.scene
    previous_frame, previous_subframe = scene.frame_current, scene.frame_subframe
    samples = []
    try:
        for target in clip.end_effector_targets:
            if not target.enabled:
                continue
            if target.target_object.name not in context.view_layer.objects:
                raise ValueError("Target Empty must belong to the current View Layer.")
            scene.frame_set(clip.start + target.target_frame)
            context.view_layer.update()
            evaluated = target.target_object.evaluated_get(context.evaluated_depsgraph_get())
            position, rotation, _scale = evaluated.matrix_world.decompose()
            if not all(math.isfinite(value) for value in (*position, *rotation)):
                raise ValueError("Target transform must contain finite values.")
            samples.append(TargetSample(
                joint_name=target.joint_name,
                target_frame=target.target_frame,
                position=tuple(position),
                rotation=tuple(rotation.normalized()),
            ))
    finally:
        scene.frame_set(previous_frame, subframe=previous_subframe)
        context.view_layer.update()
    return tuple(samples)


def capture_generation_targets(context, track, clip, *, root_offset=None, fullbody_frames=()):
    """Map Empty goals to the same canonical frame as captured full-body poses.

    Use each evaluated joint as an anchor: a stationary Empty must preserve that
    joint's canonical reference pose even for a rig with different proportions.
    No pose channels, constraints or keys are changed.
    """
    import numpy as np
    from broom.kinematics import compute_global_transforms
    from broom.ops.skeleton_geometry import estimate_skeleton_scale_ratio

    from ..animation.constraint_utils import pose_heading_at_frame
    from ..animation.retargeting.constraint_capture import user_pose_to_canonical_motion
    from ..core.generation_models import EndEffectorTarget
    from . import actions

    validate_targets(clip, fullbody_frames=fullbody_frames)
    armature = track.object
    _hierarchy, mapping, canonical_from_armature = blender_retarget_service.current_retarget_build(
        armature, blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping),
    )
    animation_data = armature.animation_data
    previous_action, previous_slot = animation_data.action, animation_data.action_slot
    scene = context.scene
    previous_frame, previous_subframe = scene.frame_current, scene.frame_subframe
    origin = None if root_offset is None else np.asarray(root_offset, dtype=float)
    result = []
    try:
        animation_data.action = track.working_action
        animation_data.action_slot = actions.find_slot(
            track.working_action, track.working_slot_handle, "Working Action",
        )
        heading = pose_heading_at_frame(
            context, armature, clip.start, source_to_bone=mapping,
        )
        if origin is None:
            _user, reference = user_pose_to_canonical_motion(context, armature, [clip.start])
            positions, _rotations = compute_global_transforms(reference)
            origin = positions[0, reference.hierarchy.joint_index('Hips')].copy()
            origin[1] = 0.0
        for sample in sample_targets(context, clip):
            scene.frame_set(clip.start + sample.target_frame)
            user_motion, reference = user_pose_to_canonical_motion(
                context, armature, [clip.start + sample.target_frame],
            )
            positions, rotations = compute_global_transforms(reference)
            joint_index = reference.hierarchy.joint_index(sample.joint_name)
            scale = estimate_skeleton_scale_ratio(user_motion.hierarchy, reference.hierarchy)
            evaluated = armature.evaluated_get(context.evaluated_depsgraph_get())
            if abs(evaluated.matrix_world.determinant()) < 1.0e-10:
                raise ValueError("Armature object transform must be invertible.")
            bone_world = evaluated.matrix_world @ evaluated.pose.bones[mapping[sample.joint_name]].matrix
            world_delta = Vector(sample.position) - bone_world.translation
            local_delta = evaluated.matrix_world.inverted().to_3x3() @ world_delta
            delta = np.asarray(canonical_from_armature) @ np.asarray(local_delta)
            position = positions[0, joint_index] + float(scale) * delta - origin
            world_from_armature = evaluated.matrix_world.to_quaternion().to_matrix()
            canonical_from_world = Matrix(canonical_from_armature) @ world_from_armature.transposed()
            from mathutils import Quaternion

            delta_rotation = Quaternion(sample.rotation).to_matrix() @ bone_world.to_quaternion().to_matrix().transposed()
            rotation = (canonical_from_world @ delta_rotation @ canonical_from_world.transposed()
                        @ Matrix(rotations[0, joint_index])).to_quaternion().normalized()
            root = positions[0, reference.hierarchy.joint_index('Hips')] - origin
            result.append(EndEffectorTarget(
                joint_name=sample.joint_name,
                target_frame=sample.target_frame,
                position=tuple(float(value) for value in position),
                rotation=tuple(float(value) for value in rotation),
                root_2d=(float(root[0]), float(root[2])),
            ))
    finally:
        animation_data.action = previous_action
        animation_data.action_slot = previous_slot
        scene.frame_set(previous_frame, subframe=previous_subframe)
        context.view_layer.update()
    return tuple(result), tuple(float(value) for value in origin), heading
