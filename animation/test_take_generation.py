"""Temporary local replacement for the future server generation response."""

from pathlib import Path

import bpy
import bpy_extras.anim_utils
import numpy as np

from . import take_actions
from .retargeting import blender_motion_writer, blender_retarget_service, workflow


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
        prepared,
        assign_to_skeleton=False,
    )

    take_curves = _curve_map(action, slot)
    working_curves = _ensure_working_curves(working_binding, take_curves)
    take_actions.materialize_take(
        working_curves,
        take_curves,
        clip.start,
        clip.end,
        take.offset,
    )
    clip.active_take_uid = take.uid
    take.progress = 1.0
    return take


def switch_take(scene, track, clip, take_uid):
    """Checkpoint the visible Take, then materialize the selected one."""
    target_take = next((take for take in clip.takes if take.uid == take_uid), None)
    if target_take is None:
        raise ValueError(f"Take {take_uid} does not belong to this Clip.")
    if target_take.uid == clip.active_take_uid:
        return target_take

    working_binding = _working_binding(track, _working_armature(track))
    active_take = _active_take(clip)
    if active_take is not None:
        _checkpoint_active_take(working_binding, active_take, clip)

    action, slot = _take_binding(target_take)
    take_curves = _curve_map(action, slot)
    take_actions.materialize_take(
        _ensure_working_curves(working_binding, take_curves),
        take_curves,
        clip.start,
        clip.end,
        target_take.offset,
    )
    clip.active_take_uid = target_take.uid
    return target_take


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
    bpy_extras.anim_utils.action_ensure_channelbag_for_slot(action, slot)
    return action, slot


def _checkpoint_active_take(working_binding, take, clip):
    action, slot = _take_binding(take)
    take_actions.checkpoint_take(
        _curve_map(working_binding[1], working_binding[2]),
        _curve_map(action, slot),
        clip.start,
        clip.end,
        take.offset,
    )


def _take_binding(take):
    action = take.take_action
    if action is None:
        raise ValueError(f"Take {take.uid} has no Take Action.")
    return action, _slot_by_handle(action, take.take_slot_handle, f"Take {take.uid}")


def _curve_map(action, slot):
    bag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(action, slot)
    return {(curve.data_path, int(curve.array_index)): curve for curve in bag.fcurves}


def _ensure_working_curves(working_binding, take_curves):
    _armature, action, slot = working_binding
    bag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(action, slot)
    curves = {(curve.data_path, int(curve.array_index)): curve for curve in bag.fcurves}
    for identity, take_curve in take_curves.items():
        if identity not in curves:
            group_name = None if take_curve.group is None else take_curve.group.name
            curves[identity] = bag.fcurves.new(
                identity[0],
                index=identity[1],
                group_name=group_name,
            )
    return curves


def _slot_by_handle(action, handle, label):
    slots = [slot for slot in action.slots if slot.handle == handle]
    if len(slots) != 1:
        raise ValueError(f"{label} Slot is missing or ambiguous.")
    return slots[0]


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
