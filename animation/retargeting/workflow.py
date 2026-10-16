"""Host-independent explicit-mapping retarget workflow."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from . import mapping


def load_bvh_motion(path) -> object:
    """Load an external BVH into a portable ``Motion`` representation."""
    import broom.io

    source_path = Path(path)
    if not source_path.is_file():
        raise ValueError(f"BVH file does not exist: {source_path}")
    return broom.io.load_bvh(source_path)


def retarget_mapped_motion(
    source_motion,
    target_hierarchy,
    joint_map: Mapping[str, str],
    *,
    scale: float | None = None,
    required_source_names=mapping.REQUIRED_SOURCE_JOINTS,
):
    """Retarget a server Motion to a validated target hierarchy.

    The source is AvaCapo's canonical hierarchy, whose root offset is zero.
    Its standing height is therefore measured from its actual rest geometry,
    including End Sites, before root translation is transferred. Target
    standing height is the global rest height of the target joint explicitly
    mapped from the source root.

    No floor alignment or contact solving is enabled here.
    """
    import broom.ops.skeleton_geometry
    import broom.retargeting.retarget

    mapping.validate_mapping(
        source_motion.hierarchy,
        target_hierarchy,
        joint_map,
        required_source_names=required_source_names,
    )
    resolved_scale = (
        broom.ops.skeleton_geometry.estimate_skeleton_scale_ratio(
            source_motion.hierarchy,
            target_hierarchy,
        )
        if scale is None
        else float(scale)
    )
    source_root_height = broom.ops.skeleton_geometry.estimate_joint_height(
        source_motion.hierarchy,
        source_motion.hierarchy.joint_index(source_motion.hierarchy.root_name),
        up_axis="Y",
        floor_height=None,
        include_end_sites=True,
    )
    return broom.retargeting.retarget.retarget_mapped_motion(
        source_motion,
        target_hierarchy,
        joint_map,
        scale=resolved_scale,
        # Use each joint's explicit rest orientation when correcting rotations.
        rotation_correction="local_orientation",
        floor_height=None,
        up_axis="Y",
        source_root_height=source_root_height,
        # Leave target_mapped_root_height unset so it is taken from the mapped
        # target joint's global rest height.
    )
