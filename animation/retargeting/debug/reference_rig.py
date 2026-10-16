"""DEBUG ONLY: append the bundled AvaCapo reference armature."""

from __future__ import annotations

from pathlib import Path


_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_AVACAPO_REFERENCE_RIG_PATH = _PLUGIN_ROOT / "assets" / "rigs" / "rigs.blend"
_AVACAPO_REFERENCE_RIG_OBJECT = "avacapo_bvh_v1"


def append_avacapo_reference_armature(scene):
    """Append the bundled reference armature to ``scene`` or reuse its object."""
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
