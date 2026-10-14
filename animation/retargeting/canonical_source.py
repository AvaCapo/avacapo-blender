"""Portable contract for AvaCapo's canonical animation source skeleton.

This topology is used only before a source clip is available: mapping rows,
deterministic rig detection and name-based Auto Mapping. At retarget time the
actual ``Motion.hierarchy`` remains the authoritative animation source.
"""

from __future__ import annotations


# Ordered depth-first topology. Parent indices refer to this stable tuple.
# The order is also the mapping-editor order: complete one leg, then the
# trunk/head, then each arm. It is deliberately not a breadth-first list.
CANONICAL_SOURCE_TOPOLOGY = (
    ("Hips", -1),
    ("LeftUpLeg", 0), ("LeftLeg", 1), ("LeftFoot", 2), ("LeftToe", 3),
    ("RightUpLeg", 0), ("RightLeg", 5), ("RightFoot", 6), ("RightToe", 7),
    ("Spine", 0), ("Spine1", 9), ("Spine2", 10),
    ("Neck", 11), ("Head", 12),
    ("LeftShoulder", 11), ("LeftArm", 14),
    ("LeftForeArm", 15), ("LeftHand", 16),
    ("RightShoulder", 11), ("RightArm", 18),
    ("RightForeArm", 19), ("RightHand", 20),
)

CANONICAL_SOURCE_ROOT_NAME = "Hips"
SOURCE_ACTION_FPS_PROPERTY = "avacapo_source_fps"
DEFAULT_SOURCE_ACTION_FPS = 30.0


def parent_name(parent_index: int) -> str | None:
    """Return the canonical parent name for one topology parent index."""
    return None if parent_index == -1 else CANONICAL_SOURCE_TOPOLOGY[parent_index][0]


def canonical_source_hierarchy():
    """Build the synthetic hierarchy used before loading a real Motion."""
    import broom

    joints = []
    for name, parent in CANONICAL_SOURCE_TOPOLOGY:
        channels = (
            (
                "Xposition", "Yposition", "Zposition",
                "Xrotation", "Yrotation", "Zrotation",
            )
            if parent == -1
            else ("Xrotation", "Yrotation", "Zrotation")
        )
        joints.append(broom.Joint(
            name=name,
            parent=parent,
            offset=(0.0, 1.0 if parent != -1 else 0.0, 0.0),
            channels=channels,
        ))
    return broom.Hierarchy(tuple(joints))
