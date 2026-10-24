"""Temporary local replacement for the future server generation response."""

from pathlib import Path

import bpy
import numpy as np

from .retargeting import blender_motion_writer, blender_retarget_service, workflow
from ..timeline import actions


# TODO(test-generation): replace this bundled BVH adapter with the server result
# pipeline. The server result must still be written into a local-time Take Action.
_TEST_BVH_PATH = Path(__file__).resolve().parents[1] / "assets" / "tmp_animations" / "blender_backflip.bvh"


def generate_test_take(scene, track, clip, *, create_take):
    """Generate one local Take Action, then materialize it into the Working Action."""
    armature = _working_armature(track)
    source_motion = _test_source_motion()
    target, joint_map, canonical_from_armature = blender_retarget_service.current_retarget_build(
        armature,
        blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping),
    )
    retargeted_motion = workflow.retarget_mapped_motion(source_motion, target, joint_map)
    prepared = blender_motion_writer.prepare_retargeted_motion(
        armature,
        retargeted_motion,
        canonical_from_armature=canonical_from_armature,
        frame_start=0.0,
        frame_step=_scene_fps(scene) * source_motion.frame_time,
    )

    working_binding = _working_binding(track, armature)
    active_take = _active_take(clip)
    if active_take is not None:
        _checkpoint_active_take(working_binding, active_take, clip)

    take = create_take()
    action, slot = _new_take_action(armature, clip, take)
    take.take_action = action
    take.take_slot_handle = int(slot.handle)
    blender_motion_writer.apply_prepared_motion(
        armature,
        action,
        slot,
        prepared,
        assign_to_skeleton=False,
    )

    actions.materialize_take(
        working_binding[1],
        working_binding[2],
        action,
        slot,
        clip.start,
        clip.end,
        take.offset,
    )
    clip.active_take_uid = take.uid
    take.progress = 1.0
    return take


def _working_armature(track):
    if track.object is None or track.object.type != "ARMATURE":
        raise ValueError("Working Object must be an armature.")
    errors = blender_retarget_service.confirmed_mapping_errors(track.object)
    if errors:
        raise ValueError(errors[0])
    return track.object


def _working_binding(track, armature):
    if track.object != armature or track.working_action is None:
        raise ValueError("Working Action binding is incomplete.")
    return armature, track.working_action, _slot_by_handle(
        track.working_action,
        track.working_slot_handle,
        "Working Action",
    )


def _new_take_action(armature, clip, take):
    action = bpy.data.actions.new(name=f"AvaCapo Take {clip.uid}.{take.uid}")
    slot = action.slots.new(armature.id_type, name=armature.name)
    actions.ensure_slot_channelbag(action, slot)
    return action, slot


def _checkpoint_active_take(working_binding, take, clip):
    action, slot = _take_binding(take)
    actions.checkpoint_take(
        working_binding[1],
        working_binding[2],
        action,
        slot,
        clip.start,
        clip.end,
        take.offset,
    )


def _take_binding(take):
    action = take.take_action
    if action is None:
        raise ValueError(f"Take {take.uid} has no Take Action.")
    return action, _slot_by_handle(action, take.take_slot_handle, f"Take {take.uid}")


def _slot_by_handle(action, handle, label):
    return actions.find_slot(action, handle, label)


def _active_take(clip):
    return next((take for take in clip.takes if take.uid == clip.active_take_uid), None)


def _test_source_motion():
    """Load the local fixture through Broom's established BVH policy."""
    source_motion = workflow.load_bvh_motion(_TEST_BVH_PATH)
    values = np.asarray(source_motion.values, dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError("Test BVH must contain only finite motion values.")
    return source_motion


def _scene_fps(scene):
    fps_base = float(scene.render.fps_base)
    fps = float(scene.render.fps)
    if not np.isfinite(fps) or not np.isfinite(fps_base) or fps <= 0.0 or fps_base <= 0.0:
        raise ValueError("Scene FPS must be positive and finite.")
    return fps / fps_base
