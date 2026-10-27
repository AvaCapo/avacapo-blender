"""Host-independent explicit-mapping retarget workflow."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from . import mapping

from broom.ops.motion_editing import repeat_pose, trim_frames
from broom.ops.resample import resample_fps

def load_bvh_motion(path) -> object:
    """Load an external BVH into a portable ``Motion`` representation."""
    import broom.io

    source_path = Path(path)
    if not source_path.is_file():
        raise ValueError(f"BVH file does not exist: {source_path}")
    return broom.io.load_bvh(source_path)


def resample_motion(motion, target_fps: float, *, duration_seconds: float):
    """Fit the requested duration to whole scene frames using Broom interpolation.

    Duration uses the clip convention (frame count / FPS). Broom resamples
    the interval between the first and last poses, so retime that interval
    to cover exactly the requested number of scene samples.
    """
    
    target_count = max(1, int(round(duration_seconds * target_fps)))
    if target_count == 1:
        return replace(trim_frames(motion, stop=1), frame_time=1.0 / target_fps)
    if motion.frame_count == 1:
        return repeat_pose(motion.hierarchy, motion.values[0], target_count, 1.0 / target_fps)

    motion = replace(
        motion,
        frame_time=(target_count - 1) / (target_fps * (motion.frame_count - 1)),
    )
    return resample_fps(motion, target_fps=target_fps)


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
    including End Sites, before root translation is transferred. The mapped
    target joint's standing height is measured above its rest-pose floor too:
    a rig may have its pelvis at the origin and its feet below zero in rest pose.

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
    target_root_name = joint_map.get(source_motion.hierarchy.root_name)
    target_root_height = (
        broom.ops.skeleton_geometry.estimate_joint_height(
            target_hierarchy,
            target_hierarchy.joint_index(target_root_name),
            up_axis="Y",
            floor_height=None,
            include_end_sites=True,
        )
        if target_root_name is not None else None
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
        target_mapped_root_height=target_root_height,
    )
