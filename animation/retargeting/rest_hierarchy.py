"""Portable compiler from plain rest-joint records to Broom ``Hierarchy``.

This module deliberately has no Blender imports. DCC adapters provide simple
records with names, parents, rest joint positions, rest matrices and optional
channel/end-site metadata. Coordinate-frame derivation remains each adapter's
responsibility and is supplied as ``skeleton_from_adapter``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import scipy.spatial.transform as scipy_transform


_ROTATION_SINGULAR_VALUE_EPSILON = 1.0e-8


def compile_rest_records(records: Sequence[Mapping[str, object]], skeleton_from_adapter):
    """Compile portable rest records into a Broom hierarchy.

    Each record requires ``name``, ``parent`` (or ``None``), ``head`` and a
    3x3 rest ``matrix``. Optional ``tail``, ``use_connect``,
    ``rotation_order`` and explicit ``channels`` preserve Blender BVH-style
    channel declarations and End Sites without imposing Blender dependency.
    """
    import broom

    ordered = _depth_first(_with_synthetic_root(records))
    coordinate_rotation = _proper_coordinate_rotation(
        skeleton_from_adapter,
        "skeleton_from_adapter",
    )
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
    children_by_name = {record["name"]: [] for record in ordered}
    for record in ordered:
        parent_name = record["parent"]
        if parent_name is not None:
            children_by_name[parent_name].append(record["name"])

    joints = []
    for record in ordered:
        name = record["name"]
        parent_name = record["parent"]
        if parent_name is None:
            parent, offset = -1, coordinate_rotation @ heads[name]
            orientation = coordinate_rotation @ rest_bases[name]
        else:
            parent = index_by_name[parent_name]
            parent_basis = rest_bases[parent_name]
            offset = parent_basis.T @ (heads[name] - heads[parent_name])
            orientation = parent_basis.T @ rest_bases[name]
        end_site_offset = None
        if not children_by_name[name] and record.get("tail") is not None:
            tail = _vector(record["tail"], f"tail[{name}]")
            end_site_offset = tuple(
                float(value) for value in rest_bases[name].T @ (tail - heads[name])
            )
        joints.append(broom.Joint(
            name=name,
            parent=parent,
            offset=tuple(float(value) for value in offset),
            channels=_channels_for_rest_record(record),
            local_orientation=_matrix_to_wxyz(orientation, f"orientation[{name}]"),
            end_site_offset=end_site_offset,
        ))
    return broom.Hierarchy(tuple(joints))


def _with_synthetic_root(records):
    """Add a BVH-style root when an adapter supplies several root joints."""
    copied = [dict(record) for record in records]
    if not copied:
        raise ValueError("Rest hierarchy has no joints.")
    names = {record.get("name") for record in copied}
    roots = [record for record in copied if record.get("parent") is None]
    if len(roots) <= 1:
        return copied

    number = 0
    root_name = f"__{number}"
    while root_name in names:
        number += 1
        root_name = f"__{number}"
    for record in roots:
        record["was_adapter_root"] = True
        record["parent"] = root_name
    return [
        {
            "name": root_name,
            "parent": None,
            "head": (0.0, 0.0, 0.0),
            "tail": (0.0, 0.0, 0.0),
            "matrix": np.eye(3),
            "channels": (),
            "use_connect": False,
            "rotation_order": "XYZ",
        },
        *copied,
    ]


def _depth_first(records):
    pending = list(records)
    if not pending:
        raise ValueError("Rest hierarchy has no joints.")
    names = set()
    roots = []
    for record in pending:
        name = record.get("name")
        parent = record.get("parent")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Rest joints need unique non-empty names.")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise ValueError(f"Invalid parent for rest joint {name!r}.")
        names.add(name)
        if parent is None:
            roots.append(record)
    missing = sorted({record["parent"] for record in pending if record["parent"] is not None} - names)
    if missing:
        raise ValueError("Rest joints reference missing parents: " + ", ".join(missing))
    if len(roots) != 1:
        raise ValueError(f"Rest hierarchy must have exactly one root joint (found {len(roots)}).")

    children = {name: [] for name in names}
    for record in pending:
        parent = record["parent"]
        if parent is not None:
            children[parent].append(record)
    ordered = []

    def visit(record):
        ordered.append(record)
        for child in children[record["name"]]:
            visit(child)

    visit(roots[0])
    if len(ordered) != len(pending):
        raise ValueError("Rest-joint hierarchy contains a cycle.")
    return ordered


def _channels_for_rest_record(record):
    explicit_channels = record.get("channels")
    if explicit_channels is not None:
        return tuple(explicit_channels)
    rotation_order = record.get("rotation_order", "XYZ")
    if not isinstance(rotation_order, str) or set(rotation_order) != {"X", "Y", "Z"}:
        rotation_order = "XYZ"
    rotation_channels = tuple(f"{axis}rotation" for axis in rotation_order)
    use_connect = record.get("use_connect", True)
    if not isinstance(use_connect, bool):
        raise ValueError(f"use_connect[{record['name']}] must be boolean.")
    # All rotation axes remain declared for the first Broom contract. Blender
    # pose rotation locks require a future explicit channel-capability policy.
    if (
        record["parent"] is None
        or record.get("was_adapter_root", False)
        or not use_connect
    ):
        return ("Xposition", "Yposition", "Zposition", *rotation_channels)
    return rotation_channels


def _proper_coordinate_rotation(value, label: str) -> np.ndarray:
    """Project a finite positive-determinant 3x3 matrix to a proper rotation."""
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
