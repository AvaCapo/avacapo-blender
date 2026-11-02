"""User-armature pose capture through AvaCapo's canonical rest skeleton.

It provides the constraint-generation boundary:
    evaluated user pose -> canonical AvaCapo Motion -> raw NPZ
"""

from .. import bvh_npz, constraint_utils
from . import blender_pose_adapter, blender_retarget_service, canonical_source, workflow


def user_pose_to_canonical_motion(context, armature, frames):
    """Retarget selected user-pose frames onto the canonical rest skeleton.

    Object and parent transforms are intentionally not included.  This matches
    the current constraint capture contract.
    """
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Constraint source must be an armature.")
    canonical_to_user = blender_retarget_service.mapping_dict(
        armature.avacapo_retarget_mapping,
    )
    canonical_to_user = constraint_utils._resolved_pose_bone_names(
        armature,
        canonical_to_user,
    )
    user_motion, _rotation_offsets = blender_pose_adapter.sample_pose_motion(
        context,
        armature,
        canonical_to_user,
        frames,
    )

    canonical_hierarchy = canonical_source.canonical_source_hierarchy()

    user_to_canonical = {
        user_bone: canonical_name
        for canonical_name, user_bone in canonical_to_user.items()
    }
    # TODO(constraint-root-policy): user rigs may have an unmapped technical
    # root above semantic Hips. Fold its evaluated transform into Hips instead
    # of requiring a second root -> Hips mapping.
    if user_motion.hierarchy.root_name not in user_to_canonical:
        raise ValueError(
            "Reverse constraint retarget has no root policy: "
            f"source root {user_motion.hierarchy.root_name!r} is not mapped."
        )
    canonical_motion = workflow.retarget_mapped_motion(
        user_motion,
        canonical_hierarchy,
        user_to_canonical,
        required_source_names=frozenset(user_to_canonical),
    )
    return user_motion, canonical_motion


def raw_constraint_npz_from_canonical_motion(canonical_motion):
    """Convert canonical Motion to NPZ arrays without rig-specific offsets."""
    return bvh_npz.convert_motion_npz(canonical_motion)
