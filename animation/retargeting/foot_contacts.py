from collections.abc import Sequence

import numpy as np

from broom.foot_lock import contact_segments, remove_short_contacts
from broom.kinematics import (
    compute_global_positions,
    compute_rest_joint_positions,
)

DEFAULT_FOOT_CONTACT_JOINTS = (
    "LeftFoot", "LeftToe", "RightFoot", "RightToe",
)


def detect_floor_contact_frames(
    motion,
    floor_y=0.0,
    joint_names: Sequence[str] = DEFAULT_FOOT_CONTACT_JOINTS,
    height_enter=0.025,
    height_exit=0.04,
    velocity_enter=0.15,
    velocity_exit=0.30,
    min_contact_duration=0.08,
):
    """
    Detect stationary near-floor intervals with hysteresis.

    Foot joints represent ankle stationary phases, not heel contacts.
    Toe joints represent toe contact candidates.

    Lengths are in motion units; velocities are motion units/second.
    Default thresholds assume motion units are meters.
    """
    dt = motion.frame_time
    
    joint_names = tuple(dict.fromkeys(joint_names))
    if not joint_names:
        raise ValueError("At least one joint must be tracked.")

    missing = [
        name for name in joint_names
        if name not in motion.hierarchy.joint_names
    ]
    if missing:
        raise ValueError(f"Missing joints: {missing}")

    thresholds = np.array([
        floor_y, height_enter, height_exit,
        velocity_enter, velocity_exit, min_contact_duration,
    ], dtype=float)

    if not np.isfinite(thresholds).all():
        raise ValueError("Parameters must be finite.")

    if (
        height_enter < 0
        or height_exit < height_enter
        or velocity_enter < 0
        or velocity_exit < velocity_enter
        or min_contact_duration < 0
    ):
        raise ValueError("Invalid contact thresholds.")

    positions = compute_global_positions(motion)
    rest_positions = compute_rest_joint_positions(motion.hierarchy)

    # Ankle heights are calibrated relative to corresponding toes.
    reference_heights = {}

    for side in ("Left", "Right"):
        toe_name = f"{side}Toe"
        foot_name = f"{side}Foot"

        if toe_name not in motion.hierarchy.joint_names:
            continue

        toe_idx = motion.hierarchy.joint_index(toe_name)

        reference_heights[toe_name] = 0.0

        if foot_name in motion.hierarchy.joint_names:
            foot_idx = motion.hierarchy.joint_index(foot_name)

            reference_heights[foot_name] = (
                rest_positions[foot_idx, 1]
                - rest_positions[toe_idx, 1]
            )

    contacts = {}

    for name in joint_names:
        if name not in reference_heights:
            raise ValueError(
                f"No rest-height calibration for joint {name!r}."
            )

        idx = motion.hierarchy.joint_index(name)
        p = positions[:, idx, :]

        # XYZ velocity in world coordinates
        if len(p) > 1:
            velocity = np.linalg.norm(
                np.gradient(p, dt, axis=0),
                axis=1,
            )
        else:
            velocity = np.zeros(len(p))

        # Height relative to calibrated support position
        height_error = (
            p[:, 1]
            - floor_y
            - reference_heights[name]
        )

        # Strict conditions to begin contact
        enter = (
            (np.abs(height_error) <= height_enter)
            & (velocity <= velocity_enter)
        )

        # Looser conditions to maintain contact
        maintain = (
            (np.abs(height_error) <= height_exit)
            & (velocity <= velocity_exit)
        )

        # Hysteresis state machine
        mask = np.zeros(motion.frame_count, dtype=bool)
        active = False

        for frame in range(motion.frame_count):
            if active:
                active = bool(maintain[frame])
            else:
                active = bool(enter[frame])

            mask[frame] = active

        # Remove short contact intervals
        min_frames = max(
            1,
            int(np.ceil(min_contact_duration / dt))
        )

        mask = remove_short_contacts(
            mask,
            min_frames=min_frames,
        )

        contacts[name] = np.flatnonzero(mask)

    return contacts
