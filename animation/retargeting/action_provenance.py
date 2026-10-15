"""Per-Action provenance for reproducible Blender retarget builds.

Mapping approval stores only source-bone to target-bone names.  A retargeted
Action stores the separate, concrete target hierarchy that was used to build
its curves, so rest-pose edits can be detected without invalidating mapping.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid


_PROPERTY_NAME = "avacapo_retarget_provenance"
_SOURCE_IDENTITY_PROPERTY = "avacapo_retarget_source_uuid"
_SCHEMA_VERSION = 1


def mapping_fingerprint(joint_map) -> str:
    return _fingerprint({"joint_map": sorted((str(key), str(value)) for key, value in joint_map.items())})


def target_hierarchy_fingerprint(target_hierarchy, canonical_from_armature) -> str:
    """Fingerprint exactly the compiled target representation."""
    return _fingerprint(
        {
            "canonical_from_armature": _matrix_values(canonical_from_armature),
            "joints": [
                {
                    "name": joint.name,
                    "parent": joint.parent,
                    "offset": [float(value) for value in joint.offset],
                    "channels": list(joint.channels),
                    "local_orientation": [float(value) for value in joint.local_orientation],
                    "end_site_offset": (
                        None
                        if joint.end_site_offset is None
                        else [float(value) for value in joint.end_site_offset]
                    ),
                }
                for joint in target_hierarchy.joints
            ],
        }
    )


def bvh_source(path) -> dict[str, str]:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"BVH source does not exist: {source_path}")
    return {
        "kind": "bvh_file",
        "path": str(source_path),
        "content_fingerprint": _file_fingerprint(source_path),
    }


def blender_action_source(armature, action) -> dict[str, str]:
    """Record a local Blender source with persistent IDs and display names."""
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Retarget source must be an armature.")
    if action is None:
        raise ValueError("Retarget source Action is required.")
    import bpy

    return {
        "kind": "blender_action",
        "armature_name": armature.name,
        "action_name": action.name,
        "armature_uuid": _ensure_unique_source_identity(armature, bpy.data.objects),
        "action_uuid": _ensure_unique_source_identity(action, bpy.data.actions),
    }


def serialized(*, target_armature, target_hierarchy, canonical_from_armature, joint_map, source) -> str:
    """Build provenance before an Action output transaction starts."""
    return json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "target_armature_name": target_armature.name,
            "target_hierarchy_fingerprint": target_hierarchy_fingerprint(
                target_hierarchy,
                canonical_from_armature,
            ),
            "mapping_fingerprint": mapping_fingerprint(joint_map),
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def write_serialized(action, value: str) -> None:
    """Store a prevalidated provenance payload on an Action."""
    action[_PROPERTY_NAME] = value


def saved_value(action) -> tuple[bool, object]:
    """Capture this module's property for a transactional Action replacement."""
    return _PROPERTY_NAME in action, action.get(_PROPERTY_NAME)


def restore_saved_value(action, saved: tuple[bool, object]) -> None:
    """Restore provenance after a failed Action replacement."""
    exists, value = saved
    if exists:
        action[_PROPERTY_NAME] = value
    elif _PROPERTY_NAME in action:
        del action[_PROPERTY_NAME]


def read(action):
    """Return validated provenance, or ``None`` for an ordinary Blender Action."""
    if action is None:
        return None
    raw = action.get(_PROPERTY_NAME)
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
        return None
    if not isinstance(value.get("source"), dict):
        return None
    return value


def current_status(action, *, target_hierarchy, canonical_from_armature, joint_map) -> str | None:
    """Return ``up_to_date`` / ``stale`` for an owned Action, else ``None``."""
    provenance = read(action)
    if provenance is None:
        return None
    if provenance["mapping_fingerprint"] != mapping_fingerprint(joint_map):
        return "stale"
    current_target = target_hierarchy_fingerprint(
        target_hierarchy,
        canonical_from_armature,
    )
    return (
        "up_to_date"
        if provenance["target_hierarchy_fingerprint"] == current_target
        else "stale"
    )


def load_source_motion(provenance, scene):
    """Reconstruct the source Motion recorded for a retargeted Action."""
    source = provenance.get("source")
    if not isinstance(source, dict):
        raise ValueError("Retargeted Action has no valid source provenance.")
    kind = source.get("kind")
    if kind == "bvh_file":
        path = Path(source.get("path", ""))
        if not path.is_file():
            raise ValueError(f"Original BVH source is unavailable: {path}")
        if source.get("content_fingerprint") != _file_fingerprint(path):
            raise ValueError("Original BVH source changed; create a new retarget instead.")
        from . import workflow

        return workflow.load_bvh_motion(path)
    if kind == "blender_action":
        import bpy

        armature = _resolve_source_datablock(
            bpy.data.objects,
            source.get("armature_uuid"),
            source.get("armature_name"),
            "armature",
        )
        action = _resolve_source_datablock(
            bpy.data.actions,
            source.get("action_uuid"),
            source.get("action_name"),
            "Action",
        )
        if armature.type != "ARMATURE":
            raise ValueError("Recorded Blender source armature is no longer an armature.")
        from . import blender_source_adapter

        return blender_source_adapter.motion_from_avacapo_action(armature, action, scene)
    # TODO(server-retarget): resolve a server animation by its immutable remote
    # ID plus an explicit local cache/download contract. Do not use Blender
    # source UUIDs for that provenance kind.
    raise ValueError(f"Unsupported retarget source kind: {kind!r}")


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_unique_source_identity(data_block, data_blocks) -> str:
    """Return a persistent local UUID, replacing a copied duplicate if needed."""
    if getattr(data_block, "library", None) is not None:
        raise ValueError(
            f"Source {data_block.name!r} is linked and cannot receive a persistent local ID. "
            "Make it local before retargeting."
        )
    value = data_block.get(_SOURCE_IDENTITY_PROPERTY)
    matches = [
        candidate
        for candidate in data_blocks
        if candidate.get(_SOURCE_IDENTITY_PROPERTY) == value
    ] if isinstance(value, str) and value else []
    if len(matches) == 1 and matches[0] == data_block:
        return value
    value = str(uuid.uuid4())
    data_block[_SOURCE_IDENTITY_PROPERTY] = value
    return value


def _resolve_source_datablock(data_blocks, recorded_uuid, recorded_name, label: str):
    """Resolve current Blender data by persistent UUID.

    The readable name only disambiguates an invalid duplicate UUID.
    """
    if not isinstance(recorded_uuid, str) or not recorded_uuid:
        raise ValueError(
            f"Original Blender source {label} has no persistent ID. "
            "Create a new retargeted Action."
        )
    matches = [
        candidate
        for candidate in data_blocks
        if candidate.get(_SOURCE_IDENTITY_PROPERTY) == recorded_uuid
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        named = [candidate for candidate in matches if candidate.name == recorded_name]
        if len(named) == 1:
            return named[0]
        raise ValueError(
            f"Original Blender source {label} ID is ambiguous; choose the source again "
            "and create a new retarget."
        )
    raise ValueError(
        f"Original Blender source {label} is unavailable by its persistent ID."
    )


def _fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _matrix_values(matrix) -> list[list[float]]:
    return [
        [float(matrix[row][column]) for column in range(3)]
        for row in range(3)
    ]
