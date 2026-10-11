"""Blender-only boundary for compiling an armature to a Broom hierarchy.

The common ``mapping`` and ``workflow`` modules deliberately do not import
this module.  A Maya adapter can provide the same output by calling
``compile_rest_records`` with its own rest-bone records.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import scipy.spatial.transform as scipy_transform


_ROTATION_SINGULAR_VALUE_EPSILON = 1.0e-8
_ROOT_CHANNELS = (
    "Xposition", "Yposition", "Zposition",
    "Xrotation", "Yrotation", "Zrotation",
)
_ROTATION_CHANNELS = ("Xrotation", "Yrotation", "Zrotation")
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


def armature_to_hierarchy(armature, skeleton_from_armature):
    """Compile all Blender rest bones into a Broom 0.2 ``Hierarchy``.

    ``skeleton_from_armature`` is an explicit proper 3x3 rotation ``G`` from
    Blender armature-data coordinates to server/Broom coordinates.
    """
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Retarget target must be an armature.")
    return compile_rest_records(_rest_records_from_armature(armature), skeleton_from_armature)


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


def capture_rest_bases(armature, basis_entries) -> None:
    """Persist the full rest skeleton used by a confirmed mapping."""
    _require_armature(armature)
    basis_entries.clear()
    for bone in sorted(armature.data.bones, key=lambda item: item.name):
        entry = basis_entries.add()
        entry.bone_name = bone.name
        entry.parent_name = bone.parent.name if bone.parent else ""
        entry.head = tuple(float(value) for value in bone.head_local)
        entry.matrix = tuple(
            float(bone.matrix_local[row][column])
            for row in range(3)
            for column in range(3)
        )


def rest_basis_errors(armature, basis_entries) -> list[str]:
    """Return legacy rest-skeleton mismatch errors for a confirmed mapping."""
    _require_armature(armature)
    stored_names = {entry.bone_name for entry in basis_entries if entry.bone_name}
    current_names = {bone.name for bone in armature.data.bones}
    if current_names != stored_names:
        return ["Armature bones changed since the mapping was confirmed."]
    entries_by_name = {entry.bone_name: entry for entry in basis_entries}
    for bone in armature.data.bones:
        entry = entries_by_name[bone.name]
        parent_name = bone.parent.name if bone.parent else ""
        if entry.parent_name != parent_name:
            return ["Armature hierarchy changed since the mapping was confirmed."]
        if any(abs(float(bone.head_local[axis]) - entry.head[axis]) > 1.0e-5 for axis in range(3)):
            return ["Armature rest joint positions changed since the mapping was confirmed."]
        saved = np.asarray(entry.matrix, dtype=np.float64).reshape((3, 3))
        current = np.asarray(
            [[bone.matrix_local[row][column] for column in range(3)] for row in range(3)],
            dtype=np.float64,
        )
        if np.max(np.abs(current - saved)) > 1.0e-5:
            return ["Armature T-pose changed since the mapping was confirmed."]
    return []


def compile_rest_records(records: Sequence[Mapping[str, object]], skeleton_from_armature):
    """Compile portable rest-bone records into a Broom hierarchy.

    Records contain ``name``, ``parent`` (or ``None``), ``head`` and a 3x3
    Blender rest matrix. Offsets are represented in parent rest-local axes and
    every joint receives its actual static local orientation.
    """
    import broom
    ordered = _parent_first(records)
    G = _proper_coordinate_rotation(skeleton_from_armature, "skeleton_from_armature")
    index_by_name = {record["name"]: index for index, record in enumerate(ordered)}
    heads = {
        record["name"]: _vector(record["head"], f"head[{record['name']}]")
        for record in ordered
    }
    rest_bases = {
        record["name"]: _proper_coordinate_rotation(
            record["matrix"],
            f"matrix[{record['name']}]",
        )
        for record in ordered
    }
    joints = []
    for record in ordered:
        name = record["name"]
        parent_name = record["parent"]
        if parent_name is None:
            parent, offset, channels = -1, G @ heads[name], _ROOT_CHANNELS
            orientation = G @ rest_bases[name]
        else:
            parent = index_by_name[parent_name]
            parent_basis = rest_bases[parent_name]
            offset = parent_basis.T @ (heads[name] - heads[parent_name])
            channels = _ROTATION_CHANNELS
            orientation = parent_basis.T @ rest_bases[name]
        joints.append(broom.Joint(
            name=name,
            parent=parent,
            offset=tuple(float(value) for value in offset),
            channels=channels,
            local_orientation=_matrix_to_wxyz(orientation, f"orientation[{name}]"),
        ))
    return broom.Hierarchy(tuple(joints))


def _rest_records_from_armature(armature) -> list[dict[str, object]]:
    return [
        {
            "name": bone.name,
            "parent": bone.parent.name if bone.parent else None,
            "head": tuple(float(value) for value in bone.head_local),
            "matrix": tuple(
                tuple(float(bone.matrix_local[row][column]) for column in range(3))
                for row in range(3)
            ),
        }
        for bone in _parent_first_bones(armature.data.bones)
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


def _parent_first_bones(bones):
    pending = list(bones)
    ordered, known = [], set()
    while pending:
        ready = [bone for bone in pending if bone.parent is None or bone.parent.name in known]
        if not ready:
            raise ValueError("Armature hierarchy contains an unresolved parent.")
        for bone in ready:
            ordered.append(bone)
            known.add(bone.name)
            pending.remove(bone)
    return ordered


def _parent_first(records):
    pending = list(records)
    if not pending:
        raise ValueError("Armature has no bones.")
    names = set()
    roots = 0
    for record in pending:
        name = record.get("name")
        parent = record.get("parent")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Rest bones need unique non-empty names.")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise ValueError(f"Invalid parent for rest bone {name!r}.")
        names.add(name)
        roots += parent is None
    if roots != 1:
        raise ValueError(f"Armature must have exactly one root bone (found {roots}).")

    ordered, known = [], set()
    while pending:
        ready = [record for record in pending if record["parent"] is None or record["parent"] in known]
        if not ready:
            missing = sorted({record["parent"] for record in pending} - names)
            if missing:
                raise ValueError("Rest bones reference missing parents: " + ", ".join(missing))
            raise ValueError("Rest-bone hierarchy contains a cycle.")
        for record in ready:
            ordered.append(record)
            known.add(record["name"])
            pending.remove(record)
    return ordered


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


def _matrix_to_wxyz(matrix, label: str) -> tuple[float, float, float, float]:
    rotation = _proper_coordinate_rotation(matrix, label)
    xyzw = scipy_transform.Rotation.from_matrix(rotation).as_quat()
    return tuple(float(value) for value in (xyzw[3], xyzw[0], xyzw[1], xyzw[2]))


def _vector(value, label: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64)
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise ValueError(f"{label} must be a finite 3-vector.")
    return vector
