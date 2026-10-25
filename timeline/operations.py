"""Compound Timeline commands that keep Track metadata and Action windows consistent."""

import math

import bpy

from ..core import takes, tracks
from ..animation.retargeting import blender_motion_writer, blender_retarget_service, workflow
from . import actions
from .properties import edit_track
from .runtime import ClipboardClip, ClipboardTake


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
            armature,
            action,
            slot,
            prepared,
            assign_to_skeleton=False,
            keyframe_type="GENERATED",
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


def switch_take(track, clip, take_uid, *, backup_working_area=False):
    """Make one immutable Take visible in the Working Action."""
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

    # Preflight the destination before any Working Action key is changed.
    actions.read_slot_curves(working_action, working_slot)
    actions.read_slot_curves(target_action, target_slot)
    if active_take is not None and active_take.take_action == target_action:
        raise ValueError("Different Takes cannot share one Take Action.")

    working_snapshot = None
    if backup_working_area:
        working_snapshot = actions.snapshot_range(
            working_action, working_slot, clip.start, clip.end,
        )

    try:
        actions.materialize_take(
            working_action, working_slot, target_action, target_slot,
            clip.start, clip.end, target_take.offset,
        )
    except Exception:
        if working_snapshot is not None:
            try:
                actions.restore_range(
                    working_action, working_slot, working_snapshot, clip.start, clip.end,
                )
            except Exception as rollback_error:
                raise RuntimeError("Could not restore Working Action after switching Take.") from rollback_error
        raise

    clip.active_take_uid = target_take.uid
    return target_take


def resize_start(track, clip, requested_start):
    """Resize a Clip's left edge while preserving its active Take data."""
    return _resize(track, clip, tracks.resize_start, requested_start)


def resize_end(track, clip, requested_end):
    """Resize a Clip's right edge while preserving its active Take data."""
    return _resize(track, clip, tracks.resize_end, requested_end)


def set_duration(track, clip, duration):
    """Set a Clip duration while preserving its active Take data."""
    return _resize(track, clip, tracks.set_duration, duration)


def set_take_offset(track, clip, requested_offset):
    """Move the active Take's local window without losing Working Action edits."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")
    take = takes.current_take(clip)
    if take is None:
        raise ValueError("Clip has no active Take.")

    old_offset = int(take.offset)
    new_offset = int(requested_offset)
    if new_offset == old_offset:
        return False

    working_action = working_slot = take_action = take_slot = None
    try:
        working_action, working_slot = _working_binding(track, track.object)
        take_action, take_slot = _take_binding(take)
        if take_action == working_action:
            raise ValueError("A Take Action cannot also be the Working Action.")
        actions.read_slot_curves(working_action, working_slot)
        actions.read_slot_curves(take_action, take_slot)

        working_snapshot = actions.snapshot_range(
            working_action, working_slot, clip.start, clip.end,
        )
        edit_track(track, _set_take_offset, clip.uid, take.uid, new_offset)
        actions.materialize_take(
            working_action, working_slot, take_action, take_slot,
            clip.start, clip.end, new_offset,
        )
    except Exception:
        try:
            if working_action is not None and 'working_snapshot' in locals():
                actions.restore_range(
                    working_action, working_slot, working_snapshot, clip.start, clip.end,
                )
            edit_track(track, _set_take_offset, clip.uid, take.uid, old_offset)
        except Exception as rollback_error:
            raise RuntimeError("Could not restore Actions after changing Take offset.") from rollback_error
        raise
    return True


def delete_takes(track, clip):
    """Remove all Take-owned resources and clear this Clip's Working window."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")
    resources = [(take.take_action, take.source_motion_text) for take in clip.takes]
    cleared = _clear_clip_window(track, clip)
    try:
        edit_track(track, takes.delete_takes, clip)
    except Exception:
        _restore_cleared_window(cleared, clip)
        raise
    _remove_take_resources(track.working_action, resources)


def delete_clip(track, clip):
    """Clear this Clip's Working window, then remove its Takes and metadata."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")
    resources = [(take.take_action, take.source_motion_text) for take in clip.takes]
    cleared = _clear_clip_window(track, clip)
    try:
        edit_track(track, takes.delete_takes, clip)
        if not edit_track(track, tracks.remove_clip, clip.uid):
            raise RuntimeError("Could not remove Clip metadata.")
    except Exception:
        _restore_cleared_window(cleared, clip)
        raise
    _remove_take_resources(track.working_action, resources)


def bake_clip(track, clip):
    """Keep visible Working Action keys and remove this Clip's Take ownership."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")
    working_action, working_slot = _working_binding(track, track.object)
    actions.bake_generated_range(
        working_action,
        working_slot,
        clip.start,
        clip.end,
    )
    resources = [(take.take_action, take.source_motion_text) for take in clip.takes]
    edit_track(track, takes.delete_takes, clip)
    if not edit_track(track, tracks.remove_clip, clip.uid):
        raise RuntimeError("Could not remove Clip metadata.")
    _remove_take_resources(track.working_action, resources)


def copy_clip(clip):
    """Copy Clip metadata and independent Take resources into the runtime clipboard."""
    copied_takes = {}
    try:
        for take in clip.takes:
            action, slot = _take_binding(take)
            copied_action, copied_slot_handle, copied_text = _copy_take_resource(
                action, slot, take.source_motion_text, f"Copied Take {take.uid}",
            )
            copied_takes[take.uid] = ClipboardTake(
                copied_action, copied_slot_handle, copied_text,
            )
        return ClipboardClip(tracks.serialize_clip(clip), copied_takes)
    except Exception:
        _remove_take_resources(None, (
            (item.action, item.source_text) for item in copied_takes.values()
        ))
        raise


def paste_clip(track, clipboard, start):
    """Paste a ClipboardClip with new, independent Take Actions and Text records."""
    if not isinstance(clipboard, ClipboardClip):
        raise ValueError("Timeline clipboard has no Take Action content.")
    payload = clipboard.data
    if not tracks.is_free(track.clips, start, start + payload.length):
        raise ValueError("Not enough free space for the copied Clip.")

    working_action, working_slot = _working_binding(track, track.object)
    actions.read_slot_curves(working_action, working_slot)
    copied_resources = {}
    clip = None
    try:
        for take_data in payload.takes:
            template = clipboard.takes.get(take_data.uid)
            if template is None:
                raise ValueError(f"Clipboard is missing Take {take_data.uid} resources.")
            action, slot_handle, text = _copy_take_resource(
                template.action,
                actions.find_slot(
                    template.action, template.slot_handle, f"Clipboard Take {take_data.uid}",
                ),
                template.source_text,
                f"Pasted Take {take_data.uid}",
            )
            copied_resources[take_data.uid] = (action, slot_handle, text)

        clip = edit_track(track, tracks.paste_clip, payload, start)
        if clip is None:
            raise ValueError("Not enough free space for the copied Clip.")
        for take in clip.takes:
            action, slot_handle, text = copied_resources[take.uid]
            take.take_action = action
            take.take_slot_handle = slot_handle
            take.source_motion_text = text

        active_take = takes.current_take(clip)
        if active_take is not None:
            action, slot = _take_binding(active_take)
            actions.read_slot_curves(action, slot)
            working_snapshot = actions.snapshot_range(
                working_action, working_slot, clip.start, clip.end,
            )
            actions.materialize_take(
                working_action, working_slot, action, slot,
                clip.start, clip.end, active_take.offset,
            )
        return clip
    except Exception:
        try:
            if 'working_snapshot' in locals():
                actions.restore_range(
                    working_action, working_slot, working_snapshot, clip.start, clip.end,
                )
            if clip is not None:
                edit_track(track, tracks.remove_clip, clip.uid)
            _remove_take_resources(
                working_action,
                ((action, text) for action, _slot_handle, text in copied_resources.values()),
            )
        except Exception as rollback_error:
            raise RuntimeError("Could not restore Timeline after pasting Clip.") from rollback_error
        raise


def _resize(track, clip, metadata_operation, value):
    """Apply one core resize and keep every affected active Take recoverable."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")

    initial = edit_track(track, tracks.snapshot)
    if not edit_track(track, metadata_operation, clip.uid, value):
        raise ValueError("Could not resize Clip.")
    changed = [
        candidate for candidate in track.clips
        if _range_changed(initial[candidate.uid], candidate)
    ]
    if not changed:
        return False

    active_clips = [
        candidate for candidate in changed
        if takes.current_take(candidate) is not None
    ]
    # An empty Clip owns no animation window.  Its resize must not alter keys
    # that may have been authored independently in the Working Action.
    if not active_clips:
        return True

    try:
        working_action, working_slot = _working_binding(track, track.object)
        active = _active_take_bindings(active_clips, working_action)
        actions.read_slot_curves(working_action, working_slot)
        for _clip, _take, action, slot in active:
            actions.read_slot_curves(action, slot)

        working_start = min(
            min(initial[candidate.uid].start, candidate.start)
            for candidate in active_clips
        )
        working_end = max(
            max(initial[candidate.uid].end, candidate.end)
            for candidate in active_clips
        )
        working_snapshot = actions.snapshot_range(
            working_action, working_slot, working_start, working_end,
        )
        for candidate, take, action, slot in active:
            actions.materialize_take(
                working_action, working_slot, action, slot,
                candidate.start, candidate.end, take.offset,
            )

        for start, end in _released_ranges(initial, active_clips, track.clips):
            actions.clear_range(working_action, working_slot, start, end)
    except Exception:
        try:
            if 'working_snapshot' in locals():
                actions.restore_range(
                    working_action, working_slot, working_snapshot, working_start, working_end,
                )
            edit_track(track, tracks.restore_snapshot, initial)
        except Exception as rollback_error:
            raise RuntimeError("Could not restore Actions after resizing Clip.") from rollback_error
        raise
    return True


def _active_take_bindings(clips, working_action):
    bindings = []
    seen_actions = set()
    for clip in clips:
        take = takes.current_take(clip)
        if take is None:
            continue
        action, slot = _take_binding(take)
        if action == working_action:
            raise ValueError("A Take Action cannot also be the Working Action.")
        if action.as_pointer() in seen_actions:
            raise ValueError("Different Takes cannot share one Take Action.")
        seen_actions.add(action.as_pointer())
        bindings.append((clip, take, action, slot))
    return bindings


def _set_take_offset(store, clip_uid, take_uid, offset):
    clip = tracks.find_clip(store.clips, clip_uid)
    take = takes.find_take(clip, take_uid) if clip is not None else None
    if take is None:
        raise ValueError("Take does not belong to this Clip.")
    take.offset = int(offset)


def _clear_clip_window(track, clip):
    """Clear one Clip range with rollback if Blender rejects the write."""
    working_action, working_slot = _working_binding(track, track.object)
    actions.read_slot_curves(working_action, working_slot)
    snapshot = actions.snapshot_range(
        working_action, working_slot, clip.start, clip.end,
    )
    try:
        actions.clear_range(working_action, working_slot, clip.start, clip.end)
    except Exception:
        try:
            actions.restore_range(
                working_action, working_slot, snapshot, clip.start, clip.end,
            )
        except Exception as rollback_error:
            raise RuntimeError("Could not restore Working Action after clearing Clip.") from rollback_error
        raise
    return working_action, working_slot, snapshot


def _restore_cleared_window(cleared, clip):
    working_action, working_slot, snapshot = cleared
    try:
        actions.restore_range(
            working_action, working_slot, snapshot, clip.start, clip.end,
        )
    except Exception as rollback_error:
        raise RuntimeError("Could not restore Working Action after deleting Clip.") from rollback_error


def _remove_take_resources(working_action, resources):
    """Remove only unreferenced Take resources, never the Working Action."""
    actions_to_remove = []
    texts_to_remove = []
    for action, text in resources:
        if action is not None and action != working_action and action not in actions_to_remove:
            actions_to_remove.append(action)
        if text is not None and text not in texts_to_remove:
            texts_to_remove.append(text)
    for action in actions_to_remove:
        _remove_if_unused(bpy.data.actions, action)
    for text in texts_to_remove:
        _remove_if_unused(bpy.data.texts, text)


def _slot_index(action, slot):
    for index, candidate in enumerate(action.slots):
        if candidate.handle == slot.handle:
            return index
    raise ValueError("Take Action Slot is missing.")


def _slot_at_index(action, index, label):
    try:
        return action.slots[index]
    except IndexError:
        raise ValueError(f"{label} Slot is missing.") from None


def _copy_take_resource(action, slot, source_text, label):
    copied_action = copied_text = None
    try:
        copied_action = action.copy()
        copied_slot = _slot_at_index(copied_action, _slot_index(action, slot), label)
        copied_text = None if source_text is None else source_text.copy()
        return copied_action, int(copied_slot.handle), copied_text
    except Exception:
        _remove_if_unused(bpy.data.actions, copied_action)
        _remove_if_unused(bpy.data.texts, copied_text)
        raise


def _range_changed(initial, clip):
    return initial.start != clip.start or initial.end != clip.end


def _released_ranges(initial, changed, clips):
    occupied = sorted((clip.start, clip.end) for clip in clips)
    ranges = []
    for clip in changed:
        old = initial[clip.uid]
        ranges.extend(_subtract_range(old.start, old.end, clip.start, clip.end))
    for start, end in ranges:
        for occupied_start, occupied_end in occupied:
            if end <= occupied_start or start >= occupied_end:
                continue
            if start < occupied_start:
                yield start, occupied_start
            start = max(start, occupied_end)
            if start >= end:
                break
        if start < end:
            yield start, end


def _subtract_range(start, end, keep_start, keep_end):
    if start < keep_start:
        yield start, min(end, keep_start)
    if keep_end < end:
        yield max(start, keep_end), end


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
