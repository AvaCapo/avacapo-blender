"""Host-independent explicit-mapping Broom retarget workflow."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from . import mapping


def load_bvh_motion(path) -> object:
    """Load an external BVH into Broom's portable ``Motion`` representation."""
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

    The first vertical slice uses Broom's rest-height ratio when ``scale`` is
    omitted and deliberately performs no floor alignment or contact solving.
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
    return broom.retargeting.retarget.retarget_mapped_motion(
        source_motion,
        target_hierarchy,
        joint_map,
        scale=resolved_scale,
        # DEBUG: bypass Broom's current position-only rest-frame heuristic.
        # This is not the final policy: once Broom uses real
        # Joint.local_orientation rest frames, restore rest-pose correction.
        rotation_correction="local_orientation",
        floor_height=None,
    )
