"""Host-independent validation for an explicit source-joint -> target-joint map."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping


# Server canonical skeleton topology.  Offsets are intentionally synthetic:
# Broom's name matcher needs names/topology, while actual retargeting always
# validates against the received server Motion before use.
_SERVER_TEMPLATE = (
    ("Hips", -1),
    ("LeftUpLeg", 0), ("RightUpLeg", 0), ("Spine", 0),
    ("LeftLeg", 1), ("RightLeg", 2), ("Spine1", 3),
    ("LeftFoot", 4), ("RightFoot", 5), ("Spine2", 6),
    ("LeftToe", 7), ("RightToe", 8), ("Neck", 9),
    ("LeftShoulder", 9), ("RightShoulder", 9), ("Head", 12),
    ("LeftArm", 13), ("RightArm", 14),
    ("LeftForeArm", 16), ("RightForeArm", 17),
    ("LeftHand", 18), ("RightHand", 19),
)

# Required semantic anchors for the first humanoid retargeting pass. The
# remaining server joints are optional: their absence must not block a user
# from confirming a useful mapping.
REQUIRED_SOURCE_JOINTS = frozenset({
    "Hips", "Head",
    "LeftArm", "RightArm", "LeftHand", "RightHand",
    "LeftUpLeg", "RightUpLeg", "LeftFoot", "RightFoot",
})


def canonical_source_hierarchy():
    """Return the named server skeleton used for pre-generation suggestions."""
    import broom

    joints = []
    for name, parent in _SERVER_TEMPLATE:
        channels = (
            ("Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation")
            if parent == -1 else ("Xrotation", "Yrotation", "Zrotation")
        )
        joints.append(broom.Joint(
            name=name,
            parent=parent,
            offset=(0.0, 1.0 if parent != -1 else 0.0, 0.0),
            channels=channels,
        ))
    return broom.Hierarchy(tuple(joints))


def suggest_mapping(source_hierarchy, target_hierarchy, *, min_score: float = 0.65):
    """Return Broom's name-based mapping proposal with score/reason metadata."""
    import broom.retargeting.mapping

    return broom.retargeting.mapping.map_joints(
        source_hierarchy=source_hierarchy,
        target_hierarchy=target_hierarchy,
        known_joint_map=None,
        min_score=min_score,
    )


def validate_mapping(
    source_hierarchy,
    target_hierarchy,
    joint_map: Mapping[str, str],
    *,
    required_source_names=REQUIRED_SOURCE_JOINTS,
) -> None:
    """Validate a partial map and its required semantic anchors.

    Target hierarchies may contain extra joints, because a Blender armature
    commonly has twist or helper bones. Source joints outside
    ``required_source_names`` are optional. For every source parent-child edge
    whose two endpoints are mapped, the target child must be below the target
    parent (possibly through extra target joints).
    """
    errors = mapping_errors(
        source_hierarchy,
        target_hierarchy,
        joint_map,
        required_source_names=required_source_names,
    )
    if errors:
        raise ValueError(errors[0])


def mapping_errors(
    source_hierarchy,
    target_hierarchy,
    joint_map: Mapping[str, str],
    *,
    required_source_names=REQUIRED_SOURCE_JOINTS,
) -> list[str]:
    """Return all actionable mapping errors for the editor and workflow.

    The legacy editor showed all local mapping errors before its Save button
    became available. Keep that behaviour here rather than relying on a later
    retarget operation to expose only the first invalid pair.
    """
    source_names = set(source_hierarchy.joint_names)
    target_names = set(target_hierarchy.joint_names)
    errors = []

    unknown_required = [name for name in required_source_names if name not in source_names]
    if unknown_required:
        errors.append("Required source joints are absent: " + ", ".join(sorted(unknown_required)))

    missing = [name for name in required_source_names if not joint_map.get(name)]
    if missing:
        errors.append("Required mappings: " + ", ".join(sorted(missing)))

    unknown_source = sorted(name for name in joint_map if name not in source_names)
    if unknown_source:
        errors.append("Unknown server joints: " + ", ".join(unknown_source))

    unknown_target = sorted({name for name in joint_map.values() if name not in target_names})
    if unknown_target:
        errors.append("Mapped character bones no longer exist: " + ", ".join(unknown_target))

    selected = [name for name in joint_map.values() if name in target_names]
    duplicates = sorted(name for name, count in Counter(selected).items() if count > 1)
    if duplicates:
        errors.append("A character bone is assigned more than once: " + ", ".join(duplicates))

    if unknown_source or unknown_target:
        return errors

    for source_parent, source_child, target_parent, target_child in hierarchy_relation_violations(
        source_hierarchy,
        target_hierarchy,
        joint_map,
    ):
        errors.append(
            f"Character bone {target_child!r} must be below {target_parent!r}, "
            f"because server joint {source_child!r} is below {source_parent!r}."
        )
    return errors


def invalid_source_joints(
    source_hierarchy,
    target_hierarchy,
    joint_map: Mapping[str, str],
    *,
    required_source_names=REQUIRED_SOURCE_JOINTS,
) -> frozenset[str]:
    """Return source rows that the UI should mark as invalid."""
    invalid = {
        name
        for name in required_source_names
        if name in source_hierarchy.joint_names and not joint_map.get(name)
    }
    target_to_sources = {}
    for source_name, target_name in joint_map.items():
        target_to_sources.setdefault(target_name, []).append(source_name)
    for sources in target_to_sources.values():
        if len(sources) > 1:
            invalid.update(sources)
    for source_parent, source_child, _target_parent, _target_child in hierarchy_relation_violations(
        source_hierarchy,
        target_hierarchy,
        joint_map,
    ):
        invalid.update((source_parent, source_child))
    return frozenset(invalid)


def hierarchy_relation_violations(source_hierarchy, target_hierarchy, joint_map: Mapping[str, str]):
    """Return mapped source parent-child edges invalid on the target hierarchy."""
    target_names = set(target_hierarchy.joint_names)
    violations = []
    # TODO(retarget transfer): validate real animation transfer both with and
    # without this guard. Extra target helpers/controls may be legitimate, and
    # the final policy must be based on observed transferred motion.
    for source_joint in source_hierarchy.joints:
        if source_joint.parent == -1:
            continue
        source_parent = source_hierarchy.joint_name(source_joint.parent)
        source_child = source_joint.name
        if source_parent not in joint_map or source_child not in joint_map:
            continue
        target_parent = joint_map[source_parent]
        target_child = joint_map[source_child]
        if (
            target_parent in target_names
            and target_child in target_names
            and not _is_descendant(target_hierarchy, target_child, target_parent)
        ):
            violations.append((source_parent, source_child, target_parent, target_child))
    return tuple(violations)


def _is_descendant(hierarchy, child_name: str, ancestor_name: str) -> bool:
    child_index = hierarchy.joint_index(child_name)
    ancestor_index = hierarchy.joint_index(ancestor_name)
    parent = hierarchy.parent(child_index)
    while parent != -1:
        if parent == ancestor_index:
            return True
        parent = hierarchy.parent(parent)
    return False
