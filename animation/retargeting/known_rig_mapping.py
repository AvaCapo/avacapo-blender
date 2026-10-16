"""Deterministic mappings for supported target skeleton naming conventions."""

from __future__ import annotations


_MIXAMO_TARGET_NAMES = {
    "Hips": "hips",
    "Spine": "spine",
    "Spine1": "spine1",
    "Spine2": "spine2",
    "Neck": "neck",
    "Head": "head",
    "LeftShoulder": "leftshoulder",
    "LeftArm": "leftarm",
    "LeftForeArm": "leftforearm",
    "LeftHand": "lefthand",
    "RightShoulder": "rightshoulder",
    "RightArm": "rightarm",
    "RightForeArm": "rightforearm",
    "RightHand": "righthand",
    "LeftUpLeg": "leftupleg",
    "LeftLeg": "leftleg",
    "LeftFoot": "leftfoot",
    "LeftToe": "lefttoebase",
    "RightUpLeg": "rightupleg",
    "RightLeg": "rightleg",
    "RightFoot": "rightfoot",
    "RightToe": "righttoebase",
}

# Blender's standard human metarig uses one extra torso bone compared with
# the AvaCapo/Mixamo chain. ``spine.004`` remains an unmapped target helper
# between source Spine2 and Neck; source Neck/Head preserve the target chain
# endpoints at spine.005/spine.006.
_RIGIFY_TARGET_NAMES = {
    "Hips": "spine",
    "Spine": "spine.001",
    "Spine1": "spine.002",
    "Spine2": "spine.003",
    "Neck": "spine.005",
    "Head": "spine.006",
    "LeftShoulder": "shoulder.l",
    "LeftArm": "upper_arm.l",
    "LeftForeArm": "forearm.l",
    "LeftHand": "hand.l",
    "RightShoulder": "shoulder.r",
    "RightArm": "upper_arm.r",
    "RightForeArm": "forearm.r",
    "RightHand": "hand.r",
    "LeftUpLeg": "thigh.l",
    "LeftLeg": "shin.l",
    "LeftFoot": "foot.l",
    "LeftToe": "toe.l",
    "RightUpLeg": "thigh.r",
    "RightLeg": "shin.r",
    "RightFoot": "foot.r",
    "RightToe": "toe.r",
}


def deterministic_mapping(source_hierarchy, target_hierarchy):
    """Return ``(rig_kind, source_to_target)`` for a complete known map.

    This intentionally makes no fuzzy or partial proposal. The caller must
    still validate the resulting map against the actual hierarchy.
    """
    source_names = tuple(source_hierarchy.joint_names)
    target_names = set(target_hierarchy.joint_names)
    exact = {name: name for name in source_names if name in target_names}
    if len(exact) == len(source_names):
        return "AvaCapo", exact

    canonical_targets = _unique_canonical_target_names(target_hierarchy.joint_names)
    mixamo = _complete_named_mapping(
        source_names,
        canonical_targets,
        _MIXAMO_TARGET_NAMES,
    )
    if mixamo is not None:
        return "Mixamo", mixamo

    rigify = _complete_named_mapping(
        source_names,
        canonical_targets,
        _RIGIFY_TARGET_NAMES,
    )
    if rigify is not None:
        return "Rigify", rigify
    return None


def _complete_named_mapping(source_names, canonical_targets, target_names):
    result = {}
    for source_name in source_names:
        target_canonical_name = target_names.get(source_name)
        target_name = (
            None
            if target_canonical_name is None
            else canonical_targets.get(target_canonical_name)
        )
        if target_name is None:
            return None
        result[source_name] = target_name
    return result


def _unique_canonical_target_names(target_names):
    result = {}
    duplicates = set()
    for target_name in target_names:
        canonical_name = _canonical_name(target_name)
        if canonical_name in result:
            duplicates.add(canonical_name)
        else:
            result[canonical_name] = target_name
    for canonical_name in duplicates:
        result.pop(canonical_name, None)
    return result


def _canonical_name(name: str) -> str:
    return name.split(":")[-1].strip().casefold()
