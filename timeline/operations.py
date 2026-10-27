"""Compound Timeline commands that keep Track metadata and Action windows consistent."""

import math
from dataclasses import dataclass, replace

import bpy

from ..common.logger import log
from ..core import takes, tracks
from ..core.generation_models import (
    ConstraintsParameters, GenerationCommand, PoseConstraint, TaskStatus, TextParameters,
)
from ..core.generation_service import GenerationService
from ..core.storage import Storage
from ..animation.retargeting import blender_motion_writer, blender_retarget_service, workflow
from . import actions
from .properties import edit_track
from .runtime import ClipboardClip, ClipboardTake, tag_redraw


GENERATION_TIMER_CALLBACKS = set()
_generation_service = None
_generation_service_token = ""
_pending_generations = {}


@dataclass
class _PendingGeneration:
    scene: object
    track_uid: int
    clip_uid: int
    command: GenerationCommand
    scene_fps: float
    status: TaskStatus = TaskStatus.QUEUED


def submit_generation(scene, track, clip, command: GenerationCommand, *, scene_fps: float) -> str:
    """Queue one server request and publish its result as a Take when it is ready."""
    global _generation_service
    global _generation_service_token

    if not Storage.api_token:
        raise ValueError("Connect to AvaCapo before generating a Take.")
    if isinstance(command.parameters, TextParameters):
        armature = _working_armature(track)
        working_action, working_slot = _working_binding(track, armature)
        active_take = takes.current_take(clip)
        take_action, take_slot = (None, None) if active_take is None else _take_binding(active_take)
        frames = actions.manual_pose_frames(
            working_action, working_slot, clip.start, clip.end,
            take_action=take_action, take_slot=take_slot,
            take_origin=clip.start + (active_take.offset if active_take is not None else 0),
        )
        if frames:
            from ..animation.constraint_utils import create_pose_constraint_npz_at_frames

            if any(abs(frame - round(frame)) > 1e-4 for frame in frames):
                raise ValueError("Move manual pose keys to whole frames before generating a Take.")
            frames = sorted({int(round(frame)) for frame in frames})
            joint_map = blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping)
            blender_retarget_service.current_retarget_build(armature, joint_map)
            constraints = []
            # Sampling uses the actual Working Action, including evaluated IK.
            # The immutable byte payload is captured before the background job starts.
            with bpy.context.temp_override(scene=scene, object=armature, active_object=armature):
                animation_data = armature.animation_data
                previous_action, previous_slot = animation_data.action, animation_data.action_slot
                try:
                    animation_data.action = working_action
                    animation_data.action_slot = working_slot
                    for frame in frames:
                        payload, _ = create_pose_constraint_npz_at_frames(
                            bpy.context, armature, [frame], source_to_bone=joint_map,
                        )
                        constraints.append(PoseConstraint(
                            motion_file=payload, source_frame=0,
                            target_frame=frame - clip.start,
                        ))
                finally:
                    animation_data.action = previous_action
                    animation_data.action_slot = previous_slot
                    bpy.context.view_layer.update()
            command = replace(command, parameters=ConstraintsParameters(
                num_frames=clip.end - clip.start, constraints=tuple(constraints),
                seed=command.parameters.seed, cycled=command.parameters.cycled,
            ))
    if _generation_service is None:
        _generation_service = GenerationService(
            api_token=Storage.api_token,
            max_concurrent=1,
        )
        _generation_service.start()
        _generation_service_token = Storage.api_token
    elif _generation_service_token != Storage.api_token:
        _generation_service.set_api_token(Storage.api_token)
        _generation_service_token = Storage.api_token

    task_id = _generation_service.submit(command)
    _pending_generations[task_id] = _PendingGeneration(
        scene=scene,
        track_uid=track.uid,
        clip_uid=clip.uid,
        command=command,
        scene_fps=scene_fps,
    )
    log.info("Queued server generation %s for Clip %s.", task_id[:8], clip.uid)
    if not bpy.app.timers.is_registered(_finish_generation):
        bpy.app.timers.register(_finish_generation, first_interval=0.1)
        GENERATION_TIMER_CALLBACKS.add(_finish_generation)
    return task_id


def _finish_generation() -> float | None:
    if _generation_service is None:
        _pending_generations.clear()
        GENERATION_TIMER_CALLBACKS.discard(_finish_generation)
        return None

    changed = False
    for task_id, pending in tuple(_pending_generations.items()):
        task = _generation_service.get_task(task_id)
        if task.status != pending.status:
            log.info("Generation %s: %s", task_id[:8], task.status.value)
            pending.status = task.status
        if task.status not in {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
            continue

        _pending_generations.pop(task_id)
        changed = True
        if task.status == TaskStatus.SUCCEEDED:
            result = _generation_service.get_result(task_id)
            try:
                if result is None:
                    raise RuntimeError("Generation completed without an animation result.")
                _publish_generation(pending, result.bvh_bytes, result.resolved_seed)
            except Exception as exc:
                log.error("Could not create Take from generated animation: %s", exc)
            finally:
                _generation_service.release_result(task_id)
        elif task.status == TaskStatus.FAILED:
            log.error("Animation generation failed: %s", task.error or "unknown error")

    if changed:
        tag_redraw()
    if _pending_generations:
        return 0.1
    GENERATION_TIMER_CALLBACKS.discard(_finish_generation)
    return None


def _publish_generation(pending: _PendingGeneration, bvh_bytes: bytes, resolved_seed: int | None) -> None:
    """Retarget one completed server result on Blender's main thread."""
    from broom.io import load_bvh_from_bytes

    track = next(
        (item for item in pending.scene.clip_data.tracks if item.uid == pending.track_uid),
        None,
    )
    if track is None:
        raise ValueError("The Track was deleted while the animation was generating.")
    clip = tracks.find_clip(track.clips, pending.clip_uid)
    if clip is None:
        raise ValueError("The Clip was deleted while the animation was generating.")

    command = pending.command
    if resolved_seed is not None and hasattr(command.parameters, "seed"):
        command = replace(command, parameters=replace(command.parameters, seed=resolved_seed))
    apply_generated_motion(
        track,
        clip,
        command=command,
        source_motion=load_bvh_from_bytes(bvh_bytes),
        source_text=bvh_bytes.decode("utf-8"),
        scene_fps=pending.scene_fps,
    )


def cancel_generation() -> None:
    """Cancel server work without waiting for a network request to return."""
    global _generation_service
    global _generation_service_token

    for callback in tuple(GENERATION_TIMER_CALLBACKS):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    GENERATION_TIMER_CALLBACKS.clear()
    _pending_generations.clear()
    if _generation_service is not None:
        _generation_service.stop(wait=False)
        _generation_service = None
    _generation_service_token = ""


def apply_generated_motion(track, clip, *, command, source_motion, source_text, scene_fps):
    """Retarget one source Motion and publish its Take only after it is usable."""
    armature = _working_armature(track)
    if not isinstance(source_text, str) or not source_text:
        raise ValueError("Generated source text is missing.")
    if not math.isfinite(scene_fps) or scene_fps <= 0.0:
        raise ValueError("Scene FPS must be positive and finite.")

    frame_step = scene_fps * source_motion.frame_time
    if isinstance(command.parameters, TextParameters):
        source_motion = workflow.resample_motion(
            source_motion, scene_fps, duration_seconds=command.parameters.duration,
        )
        frame_step = 1.0
    elif isinstance(command.parameters, ConstraintsParameters):
        # Constraint targets are clip-relative frame indices. Keep those indices
        # on the scene grid even when the returned BVH uses a different FPS.
        source_motion = workflow.resample_motion(
            source_motion, scene_fps,
            duration_seconds=command.parameters.num_frames / scene_fps,
        )
        frame_step = 1.0

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
        frame_step=frame_step,
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
    if active_take is not None:
        active_action, active_slot = _take_binding(active_take)
        actions.manual_pose_frames(
            working_action, working_slot, clip.start, clip.end,
            take_action=active_action, take_slot=active_slot,
            take_origin=clip.start + active_take.offset,
        )

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
    clip.prompt = target_take.prompt
    clip.seed = target_take.seed
    clip.generation_model = target_take.generation_model
    clip.generation_temperature = target_take.temperature
    clip.generation_cycled = target_take.cycled
    clip.generation_in_place = target_take.generation_in_place
    return target_take


def _apply_resize_deltas(track, initial, *, skip_clip_uids=()):
    """Apply Working Action resize deltas from one Track snapshot."""
    # TODO(investigate-binding-propagation): Binding should affect at most one
    # neighbour per operation. Once that invariant is explicit, replace this
    # Track-wide metadata scan with the known affected Clip.
    skipped = set(skip_clip_uids)
    changed = []
    for clip in track.clips:
        if clip.uid in skipped:
            continue
        previous = initial.get(clip.uid)
        if previous is None or (
            previous.start == clip.start and previous.end == clip.end
        ):
            continue
        take = takes.current_take(clip)
        if take is not None:
            changed.append((clip, take, previous))
    if not changed:
        return False

    working_action, working_slot = _working_binding(track, track.object)
    bindings = []
    for clip, take, previous in changed:
        action, slot = _take_binding(take)
        if action == working_action:
            raise ValueError("A Take Action cannot also be the Working Action.")
        previous_offsets = dict(previous.take_offsets)
        if take.uid not in previous_offsets:
            raise ValueError("Active Take is missing from the drag snapshot.")
        bindings.append((
            clip,
            action,
            slot,
            previous.start,
            previous.end,
            previous.start + previous_offsets[take.uid],
        ))

    for clip, action, slot, old_start, old_end, origin in bindings:
        actions.apply_resize_delta(
            working_action,
            working_slot,
            action,
            slot,
            old_start,
            old_end,
            clip.start,
            clip.end,
            origin,
        )
    return True


def create_clip(track, frame, default_length):
    """Create a Clip and apply automatic binding resizes to Working Actions."""
    initial = edit_track(track, tracks.snapshot)
    clip = edit_track(track, tracks.create_clip_at, frame, default_length)
    if clip is not None:
        _apply_resize_deltas(track, initial)
    return clip


def resize_clip(track, clip_uid, operation, value):
    """Resize from a panel command and immediately update the Working Action."""
    initial = edit_track(track, tracks.snapshot)
    if not edit_track(track, operation, clip_uid, value):
        return False
    return _apply_resize_deltas(track, initial)


def move_clip(track, clip_uid, requested_start):
    """Move one Clip and its active-Take keys from a panel Start edit."""
    clip = tracks.find_clip(track.clips, clip_uid)
    if clip is None:
        return False
    initial = edit_track(track, tracks.snapshot)
    old_start, old_end = clip.start, clip.end
    if not edit_track(track, tracks.move_clip, clip_uid, requested_start):
        return False

    delta = clip.start - old_start
    take = takes.current_take(clip)
    if take is not None and delta:
        working_action, working_slot = _working_binding(track, track.object)
        take_action, take_slot = _take_binding(take)
        if take_action == working_action:
            raise ValueError("A Take Action cannot also be the Working Action.")
        actions.translate_take_window(
            working_action,
            working_slot,
            take_action,
            take_slot,
            old_start,
            old_end,
            delta,
        )
    _apply_resize_deltas(track, initial, skip_clip_uids=(clip.uid,))
    return True


def finalize_resize_drag(track, session):
    """Apply Action changes caused by one completed Timeline drag."""
    if session.kind == 'BODY':
        return _apply_resize_deltas(
            track, session.initial, skip_clip_uids=(session.uid,),
        )
    if session.kind not in {'LEFT', 'RIGHT', 'SYNC'}:
        return False
    return _apply_resize_deltas(track, session.initial)


def set_take_offset(track, clip, requested_offset):
    """Move the active Take window without rebuilding the whole Clip range."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")
    take = takes.current_take(clip)
    if take is None:
        raise ValueError("Clip has no active Take.")

    old_offset = int(take.offset)
    new_offset = int(requested_offset)
    if new_offset == old_offset:
        return False

    working_action, working_slot = _working_binding(track, track.object)
    take_action, take_slot = _take_binding(take)
    if take_action == working_action:
        raise ValueError("A Take Action cannot also be the Working Action.")

    actions.apply_offset_delta(
        working_action,
        working_slot,
        take_action,
        take_slot,
        clip.start,
        clip.end,
        old_offset,
        new_offset,
    )
    edit_track(track, _set_take_offset, clip.uid, take.uid, new_offset)
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


def delete_take(track, clip, take_uid):
    """Delete one Take and keep the Clip's visible Action window valid."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")
    target = takes.find_take(clip, take_uid)
    if target is None:
        raise ValueError(f"Take {take_uid} does not belong to this Clip.")

    resource = (target.take_action, target.source_motion_text)
    active_take = takes.current_take(clip)
    if active_take is not None and active_take.uid == target.uid:
        replacement = next((take for take in clip.takes if take.uid != target.uid), None)
        if replacement is not None:
            switch_take(track, clip, replacement.uid)
        else:
            cleared = _clear_clip_window(track, clip)
            try:
                edit_track(track, takes.delete_takes, clip)
            except Exception:
                _restore_cleared_window(cleared, clip)
                raise
            _remove_take_resources(track.working_action, (resource,))
            return

    if not edit_track(track, takes.remove_take, clip, target.uid):
        raise RuntimeError("Could not remove Take metadata.")
    _remove_take_resources(track.working_action, (resource,))


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
