"""Production custom-rig retargeting from generated AvaCapo BVH to Blender."""

from __future__ import annotations

from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

from broom.bvh.retargeting import (
    align_root_to_floor,
    build_relational_constraints,
    semantic_skeleton_projection,
    solve_motion_spacetime,
)
from broom.bvh.schemas import BVHDocument, BVHJoint

from .logger import log
from .rig_mapping import basis_errors, canonical_from_armature_basis, mapping_dict, mapping_errors


_SEMANTIC_CHAINS = (
    ("Hips", "Spine", "Spine1", "Spine2", "Neck", "Head"),
    ("Spine2", "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand"),
    ("Spine2", "RightShoulder", "RightArm", "RightForeArm", "RightHand"),
    ("Hips", "LeftUpLeg", "LeftLeg", "LeftFoot", "LeftToe"),
    ("Hips", "RightUpLeg", "RightLeg", "RightFoot", "RightToe"),
)


def _is_descendant_of(bone: bpy.types.Bone, ancestor: bpy.types.Bone) -> bool:
    current = bone.parent
    while current is not None:
        if current == ancestor:
            return True
        current = current.parent
    return False


def validate_custom_retarget_target(target_armature: bpy.types.Object) -> None:
    """Reject targets whose evaluated Blender pose cannot match the FK model."""
    if target_armature is None or target_armature.type != "ARMATURE":
        raise ValueError("Custom retarget target must be an armature.")

    mapping = target_armature.avacapo_rig_mapping
    errors = []
    if not mapping.configured:
        errors.append("Save a custom rig mapping before applying animation.")
    errors.extend(mapping_errors(target_armature, mapping.entries))
    try:
        canonical_from_armature_basis(target_armature, mapping)
    except ValueError as exc:
        errors.append(str(exc))
    errors.extend(basis_errors(target_armature, mapping))

    roots = [bone for bone in target_armature.data.bones if bone.parent is None]
    if len(roots) != 1:
        errors.append(
            "Custom retarget currently requires exactly one root bone "
            f"(found {len(roots)})."
        )

    matrix_world = target_armature.matrix_world
    world_scale = matrix_world.to_scale()
    if not np.all(np.isfinite(tuple(world_scale))):
        errors.append("Armature object scale contains non-finite values.")
    elif max(world_scale) - min(world_scale) > 1e-5:
        errors.append("Apply non-uniform armature object scale before retargeting.")
    if matrix_world.to_3x3().determinant() <= 0.0:
        errors.append("Armature object transform must not be mirrored.")

    constrained = [bone.name for bone in target_armature.pose.bones if bone.constraints]
    if constrained:
        errors.append(
            "Custom retarget currently requires FK pose bones without constraints: "
            + ", ".join(constrained[:5])
            + ("..." if len(constrained) > 5 else "")
        )
    animation_data = target_armature.animation_data
    pose_drivers = (
        [driver for driver in animation_data.drivers if driver.data_path.startswith('pose.bones[')]
        if animation_data is not None
        else []
    )
    if pose_drivers:
        errors.append("Custom retarget currently does not support pose-bone drivers.")

    selected = mapping_dict(mapping)
    for chain in _SEMANTIC_CHAINS:
        mapped = [(name, selected[name]) for name in chain if name in selected]
        for (parent_semantic, parent_name), (child_semantic, child_name) in zip(mapped, mapped[1:]):
            parent_bone = target_armature.data.bones.get(parent_name)
            child_bone = target_armature.data.bones.get(child_name)
            if (
                parent_bone is not None
                and child_bone is not None
                and not _is_descendant_of(child_bone, parent_bone)
            ):
                errors.append(
                    f"Mapped {child_semantic} ({child_name}) must be below "
                    f"{parent_semantic} ({parent_name}) in the armature hierarchy."
                )

    if errors:
        raise ValueError("Custom retarget preflight failed: " + " ".join(errors))


def _parent_first_bones(armature: bpy.types.Object) -> list[bpy.types.Bone]:
    pending = list(armature.data.bones)
    ordered: list[bpy.types.Bone] = []
    known: set[str] = set()
    while pending:
        ready = [bone for bone in pending if bone.parent is None or bone.parent.name in known]
        if not ready:
            raise ValueError("Armature hierarchy contains an unresolved parent.")
        for bone in ready:
            ordered.append(bone)
            known.add(bone.name)
            pending.remove(bone)
    return ordered


def armature_rest_bvh(
    armature: bpy.types.Object,
    frame_count: int,
    frame_time: float | None,
    canonical_from_armature: Matrix,
) -> BVHDocument:
    """Build a zero-rotation canonical BVH from transformed rest joint points."""
    if armature.type != "ARMATURE":
        raise ValueError("Custom retarget target must be an armature.")
    bones = _parent_first_bones(armature)
    if not bones:
        raise ValueError("Target armature has no bones.")
    if frame_time is None or not np.isfinite(frame_time) or frame_time <= 0:
        raise ValueError("Generated BVH must provide a positive Frame Time.")

    indices = {bone.name: index for index, bone in enumerate(bones)}
    canonical_heads = {
        bone.name: np.asarray(canonical_from_armature @ bone.head_local, dtype=np.float64)
        for bone in bones
    }
    joints = []
    channel_start = 0
    for bone in bones:
        parent = -1 if bone.parent is None else indices[bone.parent.name]
        if parent == -1:
            offset = canonical_heads[bone.name]
            channels = (
                "Xposition", "Yposition", "Zposition",
                "Xrotation", "Yrotation", "Zrotation",
            )
        else:
            offset = canonical_heads[bone.name] - canonical_heads[bone.parent.name]
            channels = ("Xrotation", "Yrotation", "Zrotation")
        joints.append(
            BVHJoint(
                name=bone.name,
                parent=parent,
                offset=offset,
                channels=channels,
                channel_start=channel_start,
            )
        )
        channel_start += len(channels)

    return BVHDocument(
        path=Path("<blender-armature-rest-pose>"),
        prefix_lines=(
            "MOTION\n",
            f"Frames: {frame_count}\n",
            f"Frame Time: {frame_time:.8f}\n",
        ),
        motion_rows=(),
        motion_values=np.zeros((frame_count, channel_start), dtype=np.float64),
        joints=tuple(joints),
        total_channels=channel_start,
        root_name=joints[0].name,
        root_channels=joints[0].channels,
        frame_time=frame_time,
        declared_frames=frame_count,
    )


# TODO(retarget diagnostics): enable only for local Broom integration testing. 
# `retarget_diagnostics.py` is a local developer helper 
# and may intentionally be absent from Git/worktrees; 
# keep this False in normal development and production.
_EXPORT_RETARGET_BVH_ARTIFACTS = False

def retarget_to_custom_armature(
    source_document: BVHDocument,
    target_armature: bpy.types.Object,
) -> BVHDocument:
    """Retarget generated AvaCapo animation using the saved manual mapping."""
    if source_document.frame_count < 2:
        raise ValueError("Custom retargeting requires at least two generated frames.")
    validate_custom_retarget_target(target_armature)
    mapping = target_armature.avacapo_rig_mapping
    canonical_from_armature = canonical_from_armature_basis(target_armature, mapping)
    target_document = armature_rest_bvh(
        target_armature,
        source_document.frame_count,
        source_document.frame_time,
        canonical_from_armature,
    )
    selected = mapping_dict(mapping)
    joint_mapping = {
        template_name: (template_name, target_name)
        for template_name, target_name in selected.items()
        if template_name in source_document.joint_index
    }
    if not joint_mapping:
        raise ValueError("The custom rig mapping has no joints in the generated animation.")

    source_aligned = align_root_to_floor(source_document, use_rest_pose=False)
    reference = semantic_skeleton_projection(source_aligned, target_document, joint_mapping)
    reference = align_root_to_floor(reference, use_rest_pose=False)
    constraints = build_relational_constraints(source_aligned, target_document, joint_mapping)
    result = solve_motion_spacetime(
        reference,
        constraints,
        control_point_spacing=8,
        max_nfev=8,
    )

    if _EXPORT_RETARGET_BVH_ARTIFACTS:
        try:
            from .retarget_diagnostics import export_custom_retarget_artifacts

            armature_rest_document = armature_rest_bvh(
                target_armature,
                source_document.frame_count,
                source_document.frame_time,
                Matrix.Identity(3),
            )
            export_custom_retarget_artifacts(
                source_document,
                target_armature,
                armature_rest_document,
                target_document,
                reference,
                result.document,
                canonical_from_armature,
            )
        except Exception:
            log.exception("TODO(retarget diagnostics): failed to export custom-retarget artifacts")
    return result.document
