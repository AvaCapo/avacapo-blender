"""Blender-only boundary for compiling an armature to a Broom hierarchy.

The common ``mapping`` and ``workflow`` modules deliberately do not import
this module. The portable ``rest_hierarchy`` compiler accepts the records
collected here and can equally be called from a Maya adapter.
"""

from __future__ import annotations

import numpy as np

from . import rest_hierarchy


_ROTATION_SINGULAR_VALUE_EPSILON = 1.0e-8
# Source semantic directions expressed in Broom's right-handed coordinate
# system. The ordered semantic triple (right, up, front) is intentionally
# left-handed: the generated source places character-right on -X.
_SOURCE_SEMANTIC_BASIS = np.diag((-1.0, 1.0, 1.0))
_UP_DIRECTION_PAIRS = (
    ("Hips", "Head", 4.0),
    ("Hips", "Neck", 2.0),
    ("Hips", "Spine2", 1.0),
    ("Spine", "Head", 2.0),
    ("Spine2", "Head", 1.0),
)
_RIGHT_DIRECTION_PAIRS = (
    ("LeftShoulder", "RightShoulder", 3.0),
    ("LeftArm", "RightArm", 2.0),
    ("LeftUpLeg", "RightUpLeg", 3.0),
    ("LeftHand", "RightHand", 1.0),
    ("LeftFoot", "RightFoot", 1.0),
)
_BASIS_EPSILON = 1.0e-8
_BASIS_AXIS_AMBIGUITY_MARGIN = 0.15


def direct_armature_compatibility_errors(
    source_armature,
    target_armature,
    source_names,
    source_from_armature,
    target_from_armature,
    *,
    rest_position_tolerance: float = 1.0e-5,
    rest_rotation_tolerance_degrees: float = 0.01,
) -> list[str]:
    """Return direct-path rest-skeleton mismatches for two Blender armatures.

    Unlike a server BVH ``Hierarchy``, an imported Blender source armature
    retains the actual bone rest axes needed for this comparison. Both supplied
    coordinate rotations map each armature-data space into the same canonical
    source space. Target-only helper bones are allowed.
    """
    _require_armature(source_armature)
    _require_armature(target_armature)
    source_G = _proper_coordinate_rotation(source_from_armature, "source_from_armature")
    target_G = _proper_coordinate_rotation(target_from_armature, "target_from_armature")
    position_tolerance = _nonnegative_finite(
        rest_position_tolerance,
        "rest_position_tolerance",
    )
    rotation_tolerance = _nonnegative_finite(
        rest_rotation_tolerance_degrees,
        "rest_rotation_tolerance_degrees",
    )
    names = tuple(source_names)
    if not names:
        raise ValueError("Direct compatibility requires at least one source bone.")
    source_bones = source_armature.data.bones
    target_bones = target_armature.data.bones
    missing_source = [name for name in names if source_bones.get(name) is None]
    if missing_source:
        raise ValueError("Source armature is missing source bones: " + ", ".join(missing_source))
    missing_target = [name for name in names if target_bones.get(name) is None]
    if missing_target:
        return ["Target is missing source bones: " + ", ".join(missing_target)]

    source_root_names = tuple(
        name for name in names if source_bones[name].parent is None
    )
    if len(source_root_names) != 1:
        return [
            "Direct compatibility requires exactly one source root among motion bones; "
            f"found {len(source_root_names)}."
        ]
    root_name = source_root_names[0]
    source_root_head = source_G @ _vector(
        source_bones[root_name].head_local,
        f"head[{root_name}]",
    )
    target_root_head = target_G @ _vector(
        target_bones[root_name].head_local,
        f"head[{root_name}]",
    )
    errors = []
    for name in names:
        source_bone = source_bones[name]
        target_bone = target_bones[name]
        source_parent = source_bone.parent.name if source_bone.parent else None
        target_parent = target_bone.parent.name if target_bone.parent else None
        if source_parent != target_parent:
            errors.append(
                f"Bone {name!r} has parent {target_parent!r} on target; "
                f"expected {source_parent!r}."
            )
            continue

        # A rigid translation of every bone in Blender Edit Mode changes the
        # armature's root rest offset but not its skeleton geometry. Compare
        # joint locations relative to the source/target motion root instead.
        # The writer will handle the root rest offset explicitly.
        source_head = (
            source_G @ _vector(source_bone.head_local, f"head[{name}]")
            - source_root_head
        )
        target_head = (
            target_G @ _vector(target_bone.head_local, f"head[{name}]")
            - target_root_head
        )
        position_error = float(np.linalg.norm(source_head - target_head))
        if position_error > position_tolerance:
            errors.append(
                f"Rest joint position differs for {name!r}: {position_error:.6g} "
                f"(tolerance {position_tolerance:.6g})."
            )

        source_basis = source_G @ _proper_coordinate_rotation(
            _matrix3(source_bone.matrix_local),
            f"matrix[{name}]",
        )
        target_basis = target_G @ _proper_coordinate_rotation(
            _matrix3(target_bone.matrix_local),
            f"matrix[{name}]",
        )
        rotation_error = _rotation_angle_degrees(source_basis, target_basis)
        if rotation_error > rotation_tolerance:
            errors.append(
                f"Rest orientation differs for {name!r}: {rotation_error:.6g} degrees "
                f"(tolerance {rotation_tolerance:.6g})."
            )
    return errors


def armature_to_hierarchy(armature, skeleton_from_armature):
    """Compile all Blender rest bones into a Broom 0.2 ``Hierarchy``.

    ``skeleton_from_armature`` is an explicit proper 3x3 rotation ``G`` from
    Blender armature-data coordinates to server/Broom coordinates.
    """
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Retarget target must be an armature.")
    return rest_hierarchy.compile_rest_records(
        _rest_records_from_armature(armature),
        skeleton_from_armature,
    )


def canonical_from_armature_mapping(armature, joint_map) -> np.ndarray:
    """Return proper ``G`` from Armature data to the source coordinate space.

    Both source and target use the same semantic body-frame parity: their
    ordered triples ``(right, up, front)`` are left-handed. Their relative
    transform is therefore a proper rotation. No user-facing forward-axis
    choice is needed or permitted.
    """
    _require_armature(armature)
    up = _select_signed_armature_axis(
        _weighted_mapped_direction(armature, joint_map, _UP_DIRECTION_PAIRS, "up"),
        label="up",
        allowed_axes=(0, 1, 2),
    )
    right = _select_signed_armature_axis(
        _weighted_mapped_direction(armature, joint_map, _RIGHT_DIRECTION_PAIRS, "right"),
        label="right",
        allowed_axes=tuple(axis for axis in range(3) if abs(up[axis]) < 0.5),
    )
    # right × up = -front for the source semantic convention, hence front is
    # up × right. This gives target and source frames the same parity.
    forward = np.cross(up, right)
    target_character_basis = np.column_stack((right, up, forward))
    return _proper_coordinate_rotation(
        _SOURCE_SEMANTIC_BASIS @ target_character_basis.T,
        "canonical_from_armature",
    )


def _rest_records_from_armature(armature) -> list[dict[str, object]]:
    return [
        {
            "name": bone.name,
            "parent": bone.parent.name if bone.parent else None,
            "head": tuple(float(value) for value in bone.head_local),
            "tail": tuple(float(value) for value in bone.tail_local),
            "matrix": tuple(
                tuple(float(bone.matrix_local[row][column]) for column in range(3))
                for row in range(3)
            ),
            "use_connect": bool(bone.use_connect),
            "rotation_order": _bvh_rotation_order(armature.pose.bones[bone.name]),
        }
        for bone in _depth_first_bones(armature.data.bones)
    ]


def _require_armature(armature) -> None:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Retarget target must be an armature.")


def _weighted_mapped_direction(armature, joint_map, pairs, label: str) -> np.ndarray:
    direction = np.zeros(3, dtype=np.float64)
    for source_start, source_end, weight in pairs:
        target_start = joint_map.get(source_start)
        target_end = joint_map.get(source_end)
        if not target_start or not target_end:
            continue
        start_bone = armature.data.bones.get(target_start)
        end_bone = armature.data.bones.get(target_end)
        if start_bone is None or end_bone is None:
            continue
        vector = np.asarray(end_bone.head_local, dtype=np.float64) - np.asarray(
            start_bone.head_local, dtype=np.float64
        )
        length = float(np.linalg.norm(vector))
        if length > _BASIS_EPSILON:
            direction += float(weight) * vector / length
    length = float(np.linalg.norm(direction))
    if length <= _BASIS_EPSILON:
        needed = ", ".join(f"{start}->{end}" for start, end, _weight in pairs)
        raise ValueError(
            f"Cannot derive character {label} from mapped rest bones. "
            f"Need at least one usable pair: {needed}."
        )
    return direction / length


def _select_signed_armature_axis(direction, *, label: str, allowed_axes) -> np.ndarray:
    scores = sorted(
        ((abs(float(direction[axis])), axis) for axis in allowed_axes), reverse=True
    )
    best_score, best_axis = scores[0]
    runner_up_score = scores[1][0] if len(scores) > 1 else 0.0
    if best_score <= _BASIS_EPSILON:
        raise ValueError(
            f"Cannot determine character {label}: mapped direction has no usable "
            "Armature-space component."
        )
    if best_score - runner_up_score < _BASIS_AXIS_AMBIGUITY_MARGIN:
        components = ", ".join(
            f"axis[{axis}]={float(direction[axis]):+.3f}" for axis in allowed_axes
        )
        raise ValueError(
            f"Cannot determine character {label} axis unambiguously from mapped "
            f"rest bones ({components}). Align the rest pose closer to an Armature "
            "axis, then confirm the mapping again."
        )
    result = np.zeros(3, dtype=np.float64)
    result[best_axis] = 1.0 if direction[best_axis] >= 0.0 else -1.0
    return result


def _depth_first_bones(bones):
    """Match Blender BVH export order: source bone order within each sibling set."""
    bones = list(bones)
    children = {bone.name: [] for bone in bones}
    roots = []
    for bone in bones:
        if bone.parent is None:
            roots.append(bone)
        else:
            children[bone.parent.name].append(bone)

    ordered = []

    def visit(bone):
        ordered.append(bone)
        for child in children[bone.name]:
            visit(child)

    for root in roots:
        visit(root)
    if len(ordered) != len(bones):
        raise ValueError("Armature hierarchy contains an unresolved parent.")
    return ordered


def _bvh_rotation_order(pose_bone):
    rotation_mode = pose_bone.rotation_mode
    return rotation_mode if set(rotation_mode) == {"X", "Y", "Z"} else "XYZ"


def _proper_coordinate_rotation(value, label: str) -> np.ndarray:
    """Extract the nearest proper rotation from a Blender 3x3 transform.

    ``Bone.matrix_local`` is intended to carry a rotation, but real Blender
    armatures (including the imported Mixamo one) can contain tiny accumulated
    scale/shear errors. ``Joint.local_orientation`` cannot represent that
    scale, so use the polar/SVD projection instead of rejecting a valid rig
    because of floating-point noise. A singular transform or an actual
    reflection still has no unambiguous proper-rotation interpretation.
    """
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError(f"{label} must be a finite 3x3 matrix.")
    determinant = float(np.linalg.det(matrix))
    if determinant <= 0.0:
        raise ValueError(f"{label} must preserve orientation; found determinant {determinant:.6g}.")
    left, singular_values, right_transpose = np.linalg.svd(matrix)
    if float(np.min(singular_values)) <= _ROTATION_SINGULAR_VALUE_EPSILON:
        raise ValueError(f"{label} is singular and cannot define a rotation.")
    rotation = left @ right_transpose
    # Positive determinant above guarantees a proper result, but retain this
    # guard against an unexpected numerical failure in the decomposition.
    if float(np.linalg.det(rotation)) <= 0.0:
        raise ValueError(f"{label} cannot be projected to a proper rotation.")
    return rotation


def _matrix3(value) -> np.ndarray:
    return np.asarray(
        [[value[row][column] for column in range(3)] for row in range(3)],
        dtype=np.float64,
    )


def _rotation_angle_degrees(first, second) -> float:
    relative = np.asarray(second) @ np.asarray(first).T
    cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _nonnegative_finite(value, label: str) -> float:
    value = float(value)
    if not np.isfinite(value) or value < 0.0:
        raise ValueError(f"{label} must be a finite non-negative number.")
    return value


def _vector(value, label: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64)
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise ValueError(f"{label} must be a finite 3-vector.")
    return vector
