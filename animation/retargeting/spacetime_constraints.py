"""Build target-space Broom constraints for local space-time retargeting."""

#NOTE: Changes in this file is most likely to commit in main 

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from . import foot_contacts


_TOE_SOURCE_JOINTS = {
    "left": "LeftToe",
    "right": "RightToe",
}

_FOOT_SOURCE_JOINTS = {
    "left": "LeftFoot",
    "right": "RightFoot",
}

_SERVER_FLOOR_Y = 0.0
_REST_EPSILON = 1.0e-5


def pose_constraints_from_samples(target_hierarchy, samples: Iterable[tuple[int, object]]):
    """Pin every target joint to each captured user-pose frame.

    ``samples`` contains ``(clip_relative_frame, user_motion)`` pairs captured
    before the server request.  User and target motions are both in the
    canonical armature coordinate basis, so their FK positions are comparable.
    
    Preconditions:
        source_motion and initial_target_motion must have identical frame counts 
        and frame times. Both motions must be synchronized frame-by-frame.
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
    Contacts are detected from source Toe joints. A mapped target Toe is locked
    when available; otherwise the required mapped target Foot is used.
    """
    from broom.foot_lock import contact_segments
    from broom.kinematics import compute_rest_joint_positions
    

    if source_motion.frame_count != initial_target_motion.frame_count:
        raise ValueError("Source Motion and target initial guess must have equal frame counts.")

    target_hierarchy = initial_target_motion.hierarchy
    target_rest_positions = compute_rest_joint_positions(target_hierarchy)
    target_joints = {}
    for side, toe_name in _TOE_SOURCE_JOINTS.items():
        # Prefer mapped Toe for foot locking.
        # Fall back to mapped Foot (ankle) when Toe is unavailable.
        # Ankle locking is an approximation for incomplete skeleton mappings.
        if toe_name not in source_motion.hierarchy.joint_names:
            continue
        target_name = source_to_target.get(toe_name)
        if target_name not in target_hierarchy.joint_names:
            target_name = source_to_target.get(_FOOT_SOURCE_JOINTS[side])
        if target_name in target_hierarchy.joint_names:
            target_joints[side] = target_name
    if not target_joints:
        return ()

    frame_count = source_motion.frame_count
    if contact_frames is None:
        contacts_by_joint = foot_contacts.detect_floor_contact_frames(
            source_motion,
            floor_y=_SERVER_FLOOR_Y,
            joint_names=tuple(_TOE_SOURCE_JOINTS[side] for side in target_joints),
        )
        masks = {
            side: np.zeros(frame_count, dtype=bool)
            for side in target_joints
        }
        for side in target_joints:
            toe_name = _TOE_SOURCE_JOINTS[side]
            masks[side][contacts_by_joint[toe_name]] = True
    else:
        masks = _contact_masks_from_frames(contact_frames, frame_count, target_joints)

    contact_target_names = tuple(dict.fromkeys(target_joints.values()))
    contact_target_indices = [target_hierarchy.joint_index(name) for name in contact_target_names]
    rest_floor_y = min(target_rest_positions[index, 1] for index in contact_target_indices)
    floor_joint_indices = _floor_joint_indices(
        target_hierarchy,
        target_rest_positions,
        contact_target_indices,
        rest_floor_y,
    )

    stationary_constraints = []
    height_constraints = []
    floor_constraints = []
    for side, target_name in target_joints.items():
        for start, end in contact_segments(masks[side]):
            frames = np.arange(start, end + 1, dtype=np.int64)
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


