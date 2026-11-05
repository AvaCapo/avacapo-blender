"""Build target-space Broom constraints for local space-time retargeting."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np


_FOOT_SOURCE_JOINTS = {
    "left": ("LeftToe", "LeftFoot"),
    "right": ("RightToe", "RightFoot"),
}

_SERVER_FLOOR_Y = 0.0
_REST_EPSILON = 1.0e-5


def pose_constraints_from_samples(target_hierarchy, samples: Iterable[tuple[int, object]]):
    """Pin every target joint to each captured user-pose frame.

    ``samples`` contains ``(clip_relative_frame, user_motion)`` pairs captured
    before the server request.  User and target motions are both in the
    canonical armature coordinate basis, so their FK positions are comparable.
    """
    from broom.kinematics import compute_global_positions

    positions_by_joint = {name: [] for name in target_hierarchy.joint_names}
    frames = []
    for frame, motion in sorted(samples, key=lambda item: item[0]):
        frame = int(frame)
        if frames and frame == frames[-1]:
            raise ValueError(f"Duplicate user pose constraint at frame {frame}.")
        source_positions = compute_global_positions(motion)[0]
        source_indices = {
            name: motion.hierarchy.joint_index(name)
            for name in target_hierarchy.joint_names
            if name in motion.hierarchy.joint_names
        }
        frames.append(frame)
        for name, values in positions_by_joint.items():
            index = source_indices.get(name)
            if index is not None:
                values.append(source_positions[index].copy())

    frame_array = np.asarray(frames, dtype=np.int64)
    constraints = []
    for name, positions in positions_by_joint.items():
        if len(positions) != len(frame_array):
            continue
        constraints.append({
            "type": "position",
            "name": f"user_pose_{name}",
            "joint": name,
            "frames": frame_array,
            "positions": np.asarray(positions, dtype=np.float64),
            "axes": "XYZ",
        })
    return tuple(constraints)


def foot_constraints_from_motion(
    source_motion,
    initial_target_motion,
    source_to_target: Mapping[str, str],
    *,
    contact_frames: Mapping[str, Sequence[int]] | None = None,
):
    """Build target-space foot constraints from one generated motion.

    ``contact_frames`` is intentionally an optional local boundary for future
    model predictions.  It contains clip-relative frame indices by ``left`` and
    ``right`` side.  When omitted, contact frames are detected from source FK.

    ``initial_target_motion`` is the ordinary mapped retarget result. 
    A Foot+Toe source group detects each contact interval, but only its lowest
    mapped target joint is locked for that interval.
    """
    from broom.foot_lock import contact_segments
    from broom.kinematics import compute_global_positions, compute_rest_joint_positions
    

    if source_motion.frame_count != initial_target_motion.frame_count:
        raise ValueError("Source Motion and target initial guess must have equal frame counts.")

    target_hierarchy = initial_target_motion.hierarchy
    source_positions = compute_global_positions(source_motion)
    target_positions = compute_global_positions(initial_target_motion)
    target_rest_positions = compute_rest_joint_positions(target_hierarchy)
    source_groups = {
        side: np.asarray(
            [
                source_motion.hierarchy.joint_index(name)
                for name in names
                if name in source_motion.hierarchy.joint_names
            ],
            dtype=np.int64,
        )
        for side, names in _FOOT_SOURCE_JOINTS.items()
    }
    source_groups = {side: indices for side, indices in source_groups.items() if len(indices)}
    if not source_groups:
        return ()

    target_groups = {
        side: tuple(dict.fromkeys(
            source_to_target[source_name]
            for source_name in _FOOT_SOURCE_JOINTS[side]
            if source_name in source_to_target
            and source_to_target[source_name] in target_hierarchy.joint_names
        ))
        for side in source_groups
    }
    target_groups = {side: names for side, names in target_groups.items() if names}
    if not target_groups:
        return ()

    frame_count = source_motion.frame_count
    if contact_frames is None:
        masks = _detect_contacts_on_known_floor(source_motion, source_positions, source_groups)
    else:
        masks = _contact_masks_from_frames(contact_frames, frame_count, source_groups)

    mapped_foot_names = tuple(dict.fromkeys(
        name for names in target_groups.values() for name in names
    ))
    mapped_foot_indices = [target_hierarchy.joint_index(name) for name in mapped_foot_names]
    rest_floor_y = min(target_rest_positions[index, 1] for index in mapped_foot_indices)
    floor_joint_indices = _floor_joint_indices(
        target_hierarchy,
        target_rest_positions,
        mapped_foot_indices,
        rest_floor_y,
    )

    stationary_constraints = []
    height_constraints = []
    floor_constraints = []
    for side, source_indices in source_groups.items():
        if side not in target_groups:
            continue
        for start, end in contact_segments(masks[side]):
            frames = np.arange(start, end + 1, dtype=np.int64)
            target_name = min(
                target_groups[side],
                key=lambda name: target_positions[
                    frames, target_hierarchy.joint_index(name), 1
                ].mean(),
            )
            target_index = target_hierarchy.joint_index(target_name)
            if len(frames) > 1:
                stationary_constraints.append({
                    "type": "stationary",
                    "name": f"{side}_foot_stationary_{start}_{end}",
                    "joint": target_name,
                    "frames": frames,
                    "axes": "XZ",
                })

            rest_height = target_rest_positions[target_index, 1] - rest_floor_y
            if rest_height > _REST_EPSILON:
                # TODO(rest-contact-height): improve this rest-pose height heuristic.
                height_constraints.append({
                    "type": "position",
                    "name": f"{side}_foot_height_{start}_{end}",
                    "joint": target_name,
                    "frames": frames,
                    "positions": np.full((len(frames), 1), _SERVER_FLOOR_Y + rest_height),
                    "axes": "Y",
                })

    all_frames = np.arange(frame_count, dtype=np.int64)
    for index in sorted(floor_joint_indices):
        floor_constraints.append({
            "type": "floor",
            "name": f"floor_{target_hierarchy.joints[index].name}",
            "joint": target_hierarchy.joints[index].name,
            "frames": all_frames,
            "normal": (0.0, 1.0, 0.0),
            "offset": _SERVER_FLOOR_Y,
        })
    return tuple(stationary_constraints + height_constraints + floor_constraints)


def _detect_contacts_on_known_floor(source_motion, positions, source_groups):
    """Detect source contacts against the server's known Y=0 ground plane."""
    from broom.ops.skeleton_geometry import estimate_height
    from broom.foot_lock import remove_short_contacts
    skeleton_height = estimate_height(source_motion.hierarchy, include_end_sites=False)
    height_limit = max(0.035 * skeleton_height, 0.02)
    velocity_limit = max(0.025 * skeleton_height, 0.015)
    masks = {}
    for side, indices in source_groups.items():
        xz = positions[:, indices, :][:, :, (0, 2)].mean(axis=1)
        velocity = np.linalg.norm(np.diff(xz, axis=0, prepend=xz[:1]), axis=1)
        if len(velocity) > 1:
            velocity[0] = velocity[1]
        height = positions[:, indices, 1].min(axis=1)
        mask = (height <= _SERVER_FLOOR_Y + height_limit) & (velocity <= velocity_limit)
        masks[side] = remove_short_contacts(mask, min_frames=2)
    return masks


def _floor_joint_indices(target_hierarchy, rest_positions, foot_indices, rest_floor_y):
    """Keep mapped feet and eligible lower descendants above the semantic floor."""
    children = {index: [] for index in range(target_hierarchy.joint_count)}
    for index, joint in enumerate(target_hierarchy.joints):
        if joint.parent >= 0:
            children[joint.parent].append(index)

    result = set(foot_indices)
    for foot_index in foot_indices:
        foot_rest_y = rest_positions[foot_index, 1]
        pending = list(children[foot_index])
        while pending:
            child = pending.pop()
            child_rest_y = rest_positions[child, 1]
            if (child_rest_y <= foot_rest_y + _REST_EPSILON
                    and child_rest_y >= rest_floor_y - _REST_EPSILON):
                result.add(child)
            pending.extend(children[child])
    return result


def _contact_masks_from_frames(contact_frames, frame_count, source_groups):
    masks = {}
    for side in source_groups:
        frames = np.asarray(contact_frames.get(side, ()), dtype=np.int64)
        if np.any((frames < 0) | (frames >= frame_count)):
            raise ValueError(f"External {side} foot-contact frames are outside the generated motion.")
        mask = np.zeros(frame_count, dtype=bool)
        mask[frames] = True
        masks[side] = mask
    return masks


