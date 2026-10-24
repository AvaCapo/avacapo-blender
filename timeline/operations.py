"""Compound Timeline commands that keep Track metadata and Action windows consistent."""

import math

import bpy

from ..core import takes
from ..animation.retargeting import blender_motion_writer, blender_retarget_service, workflow
from . import actions
from .properties import edit_track


def apply_generated_motion(track, clip, *, command, source_motion, source_text, scene_fps):
    """Retarget one source Motion and publish its Take only after it is usable."""
    armature = _working_armature(track)
    if not isinstance(source_text, str) or not source_text:
        raise ValueError("Generated source text is missing.")
    if not math.isfinite(scene_fps) or scene_fps <= 0.0:
        raise ValueError("Scene FPS must be positive and finite.")

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
        frame_step=scene_fps * source_motion.frame_time,
    )

    # Resolve this before creating any resource owned by the new Take.
    _working_binding(track, armature)

    take_uid = clip.take_counter + 1
    action = source_record = take = None
    try:
        action, slot = _new_take_action(armature, clip.uid, take_uid)
        blender_motion_writer.apply_prepared_motion(
            armature, action, slot, prepared, assign_to_skeleton=False,
        )
        source_record = _new_source_text(clip.uid, take_uid, source_text)

        take = edit_track(
            track, takes.add_take, clip, command=command, activate=False,
        )
        take.take_action = action
        take.take_slot_handle = int(slot.handle)
        take.source_motion_text = source_record

        switch_take(track, clip, take.uid)
        take.progress = 1.0
        return take
    except Exception:
        if take is not None:
            edit_track(track, takes.remove_take, clip, take.uid)
        _remove_if_unused(bpy.data.actions, action)
        _remove_if_unused(bpy.data.texts, source_record)
        raise


def switch_take(track, clip, take_uid):
    """Make one Take visible, restoring the previous state if the switch fails."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")

    target_take = takes.find_take(clip, take_uid)
    if target_take is None:
        raise ValueError(f"Take {take_uid} does not belong to this Clip.")
    active_take = takes.current_take(clip)
    if active_take is not None and active_take.uid == target_take.uid:
        return target_take

    working_action = track.working_action
    if working_action is None:
        raise ValueError("Working Action binding is incomplete.")
    working_slot = actions.find_slot(
        working_action, track.working_slot_handle, "Working Action",
    )
    target_action, target_slot = _take_binding(target_take)
    if target_action == working_action:
        raise ValueError("A Take Action cannot also be the Working Action.")

    # Preflight every Slot before any Action is changed.
    actions.read_slot_curves(working_action, working_slot)
    actions.read_slot_curves(target_action, target_slot)
    active_action = active_slot = None
    if active_take is not None:
        active_action, active_slot = _take_binding(active_take)
        if active_action == working_action:
            raise ValueError("A Take Action cannot also be the Working Action.")
        if active_action == target_action:
            raise ValueError("Different Takes cannot share one Take Action.")
        actions.read_slot_curves(active_action, active_slot)

    working_snapshot = actions.snapshot_range(
        working_action, working_slot, clip.start, clip.end,
    )
    active_snapshot = None
    if active_take is not None:
        active_origin = clip.start + active_take.offset
        active_snapshot = actions.snapshot_range(
            active_action,
            active_slot,
            clip.start - active_origin,
            clip.end - active_origin,
        )

    try:
        if active_take is not None:
            actions.checkpoint_take(
                working_action, working_slot, active_action, active_slot,
                clip.start, clip.end, active_take.offset,
            )
        actions.materialize_take(
            working_action, working_slot, target_action, target_slot,
            clip.start, clip.end, target_take.offset,
        )
    except Exception:
        try:
            actions.restore_range(
                working_action, working_slot, working_snapshot, clip.start, clip.end,
            )
            if active_snapshot is not None:
                actions.restore_range(
                    active_action,
                    active_slot,
                    active_snapshot,
                    clip.start - active_origin,
                    clip.end - active_origin,
                )
        except Exception as rollback_error:
            raise RuntimeError("Could not restore Actions after switching Take.") from rollback_error
        raise

    clip.active_take_uid = target_take.uid
    return target_take


def _take_binding(take):
    if take.take_action is None:
        raise ValueError(f"Take {take.uid} has no Take Action.")
    return take.take_action, actions.find_slot(
        take.take_action, take.take_slot_handle, f"Take {take.uid}",
    )


def _working_armature(track):
    armature = track.object
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Working Object must be an armature.")
    errors = blender_retarget_service.confirmed_mapping_errors(armature)
    if errors:
        raise ValueError(errors[0])
    return armature


def _working_binding(track, armature):
    if track.object != armature or track.working_action is None:
        raise ValueError("Working Action binding is incomplete.")
    return track.working_action, actions.find_slot(
        track.working_action, track.working_slot_handle, "Working Action",
    )


def _new_take_action(armature, clip_uid, take_uid):
    action = bpy.data.actions.new(name=f"AvaCapo Take {clip_uid}.{take_uid}")
    slot = action.slots.new(armature.id_type, name=armature.name)
    return action, slot


def _new_source_text(clip_uid, take_uid, source_text):
    # TODO(source-motion-storage): test raw bytes saving. For now source BVH is
    # stored as Text so it is saved inside the .blend without a JSON key payload.
    record = bpy.data.texts.new(name=f"AvaCapo Source {clip_uid}.{take_uid}")
    record.write(source_text)
    return record


def _remove_if_unused(collection, datablock):
    if datablock is not None and datablock.users == 0:
        collection.remove(datablock)
