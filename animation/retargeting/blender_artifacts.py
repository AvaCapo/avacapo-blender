"""Small Blender-only artifact exports for retargeting investigation."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from . import blender_adapter, blender_source_adapter


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACTS_ROOT = _PLUGIN_ROOT / "retarget_artifacts"
# DEBUG ONLY: local reference rig for direct-compatibility fixtures. Keep this
# path independent from the legacy Config.BLEND_PATH, which is not the asset
# location used by the extension package.
_AVACAPO_REFERENCE_RIG_PATH = _PLUGIN_ROOT / "assets" / "rigs" / "rigs.blend"
_AVACAPO_REFERENCE_RIG_OBJECT = "avacapo_bvh_v1"


def export_armature_bone_names(armature) -> Path:
    """Write the selected armature's exact Blender bone names, one per line."""
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Select an armature before saving bone names.")
    directory = _ARTIFACTS_ROOT / _safe_path_component(armature.name)
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / "bone_names.txt"
    bone_names = sorted(bone.name for bone in armature.data.bones)
    output_path.write_text("\n".join(bone_names) + "\n", encoding="utf-8")
    return output_path


def append_avacapo_reference_armature(scene):
    """Append the bundled AvaCapo reference armature to ``scene``.

    DEBUG ONLY: this creates the direct-path reference fixture; it does not
    import an animation, create an Action, or modify an existing character.
    An already-linked reference object is reused instead of creating copies.
    """
    import bpy

    if scene is None:
        raise ValueError("Choose a Blender scene before adding the reference rig.")
    existing = scene.objects.get(_AVACAPO_REFERENCE_RIG_OBJECT)
    if existing is not None:
        if existing.type != "ARMATURE":
            raise ValueError(
                f"Scene object {_AVACAPO_REFERENCE_RIG_OBJECT!r} exists but is not an armature."
            )
        return existing
    if not _AVACAPO_REFERENCE_RIG_PATH.is_file():
        raise ValueError(
            "Bundled AvaCapo reference rig is missing: "
            f"{_AVACAPO_REFERENCE_RIG_PATH}"
        )
    with bpy.data.libraries.load(str(_AVACAPO_REFERENCE_RIG_PATH), link=False) as (
        data_from,
        data_to,
    ):
        if _AVACAPO_REFERENCE_RIG_OBJECT not in data_from.objects:
            raise ValueError(
                f"Reference object {_AVACAPO_REFERENCE_RIG_OBJECT!r} was not found in "
                f"{_AVACAPO_REFERENCE_RIG_PATH.name}."
            )
        data_to.objects = [_AVACAPO_REFERENCE_RIG_OBJECT]
    armature = data_to.objects[0]
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Bundled AvaCapo reference object is not an armature.")
    scene.collection.objects.link(armature)
    return armature


def export_direct_compatibility_report(source_armature, source_action, target_armature, scene) -> Path:
    """Save strict direct-path evidence without creating or changing an Action.

    DEBUG ONLY: this fixture tool exists to establish the direct-transfer
    contract. It intentionally does not fall back to mapping or retargeting.
    """
    source_motion = blender_source_adapter.motion_from_avacapo_action(
        source_armature,
        source_action,
        scene,
    )
    _require_armature(target_armature, "target")
    source_names = tuple(source_motion.hierarchy.joint_names)
    missing_target_bones = tuple(
        name for name in source_names if target_armature.data.bones.get(name) is None
    )
    if missing_target_bones:
        errors = ["Target is missing source bones: " + ", ".join(missing_target_bones)]
        source_from_armature = None
        target_from_armature = None
    else:
        identity_name_map = {name: name for name in source_names}
        source_from_armature = blender_adapter.canonical_from_armature_mapping(
            source_armature,
            identity_name_map,
        )
        target_from_armature = blender_adapter.canonical_from_armature_mapping(
            target_armature,
            identity_name_map,
        )
        errors = blender_adapter.direct_armature_compatibility_errors(
            source_armature,
            target_armature,
            source_names,
            source_from_armature,
            target_from_armature,
        )

    report = {
        "purpose": "DEBUG ONLY: strict direct AvaCapo Motion compatibility fixture.",
        "source_armature": source_armature.name,
        "source_action": source_action.name,
        "target_armature": target_armature.name,
        "source_frame_count": int(source_motion.frame_count),
        "source_frame_time": float(source_motion.frame_time),
        "source_joint_names": source_names,
        "target_from_armature": (
            None
            if target_from_armature is None
            else [[float(value) for value in row] for row in target_from_armature]
        ),
        "source_from_armature": (
            None
            if source_from_armature is None
            else [[float(value) for value in row] for row in source_from_armature]
        ),
        "direct_compatible": not errors,
        "errors": errors,
    }
    directory = (
        _ARTIFACTS_ROOT
        / "direct_compatibility"
        / _safe_path_component(target_armature.name)
    )
    directory.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    # A changed target rest pose must not overwrite an earlier fixture result.
    # The digest is stable for identical evidence and avoids timestamp-based
    # filenames, so repeated captures of the same fixture remain one file.
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
    output_path = directory / (
        f"{_safe_path_component(source_action.name)}-{fingerprint}.json"
    )
    output_path.write_text(serialized, encoding="utf-8")
    return output_path


def _require_armature(armature, role: str) -> None:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError(f"Choose a {role} armature.")


def _safe_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "unnamed_armature"
