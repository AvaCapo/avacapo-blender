"""Blender-only debug entry point for post-retarget space-time solving."""

from __future__ import annotations

import bpy

from ...core import tracks
from ...timeline import actions as timeline_actions
from ...timeline.properties import find_track
from . import (
    action_provenance,
    blender_motion_writer,
    blender_retarget_service,
    blender_source_adapter,
    constraint_capture,
    spacetime_constraints,
)
from .workflow import refine_motion_spacetime, retarget_mapped_motion


def create_debug_action(context, source_armature, source_action, target_armature, settings):
    """Create and assign a new Action with the space-time solve result."""
    if target_armature is None or target_armature.type != "ARMATURE":
        raise ValueError("Select a target armature first.")
    errors = blender_retarget_service.confirmed_mapping_errors(target_armature)
    if errors:
        raise ValueError(errors[0])

    source_motion = blender_source_adapter.motion_from_avacapo_action(
        source_armature,
        source_action,
        context.scene,
    )
    joint_map = blender_retarget_service.mapping_dict(target_armature.avacapo_retarget_mapping)
    target_hierarchy, _joint_map, canonical_from_armature = (
        blender_retarget_service.current_retarget_build(target_armature, joint_map)
    )
    pose_constraints = _active_clip_pose_constraints(
        context,
        target_armature,
        target_hierarchy,
        source_motion.frame_count,
    )
    initial_motion = retarget_mapped_motion(
        source_motion,
        target_hierarchy,
        joint_map,
    )
    result, constraints = refine_motion_spacetime(
        source_motion,
        initial_motion,
        joint_map,
        pose_constraints=pose_constraints,
        settings=settings,
    )
    prepared = blender_motion_writer.prepare_retargeted_motion(
        target_armature,
        result,
        canonical_from_armature=canonical_from_armature,
        frame_start=0.0,
        frame_step=1.0,
    )
    provenance = action_provenance.serialized(
        target_armature=target_armature,
        target_hierarchy=target_hierarchy,
        canonical_from_armature=canonical_from_armature,
        joint_map=joint_map,
        source=action_provenance.blender_action_source(source_armature, source_action),
    )
    action = bpy.data.actions.new(name=f"AvaCapo Space-Time - {source_action.name}")
    animation_data = target_armature.animation_data
    previous_action = None if animation_data is None else animation_data.action
    previous_slot = None if animation_data is None else animation_data.action_slot
    try:
        slot = blender_motion_writer.action_slot_for_object(action, target_armature)
        blender_motion_writer.apply_prepared_motion(
            target_armature,
            action,
            slot,
            prepared,
            assign_to_skeleton=True,
        )
        action_provenance.write_serialized(action, provenance)
    except Exception:
        animation_data = target_armature.animation_data
        if animation_data is not None and animation_data.action == action:
            animation_data.action = previous_action
            if previous_slot is not None:
                animation_data.action_slot = previous_slot
        if action.users == 0:
            bpy.data.actions.remove(action)
        raise
    return action, constraints


def _active_clip_pose_constraints(context, armature, target_hierarchy, frame_count):
    """Capture the same manual Clip poses used by generation, if available."""
    track = find_track(context)
    if track is None or track.object != armature:
        return ()
    clip = tracks.find_clip(track.clips, track.active_clip_uid)
    if clip is None:
        return ()
    if frame_count != clip.end - clip.start:
        raise ValueError(
            "Source Action frame count must equal the active Clip duration to use pose constraints."
        )

    working_action = track.working_action
    working_slot = timeline_actions.find_slot(
        working_action,
        track.working_slot_handle,
        "Working Action",
    )
    frames = timeline_actions.manual_pose_frames(
        working_action,
        working_slot,
        clip.start,
        clip.end,
    )
    samples = [(frame, frame - clip.start) for frame in frames]
    previous, following = tracks.neighbors(track.clips, clip.uid)
    if not samples and previous is not None and previous.end == clip.start:
        if timeline_actions.has_pose_keys(
            working_action,
            working_slot,
            previous.start,
            previous.end,
        ):
            samples = [(previous.end - 1, 0)]
    last_target = clip.end - clip.start - 1
    if following is not None and following.start == clip.end:
        if (not any(target == last_target for _, target in samples)
                and timeline_actions.has_pose_keys(
                    working_action,
                    working_slot,
                    following.start,
                    following.end,
                )):
            samples.append((following.start, last_target))
    if not samples:
        return ()
    if any(abs(frame - round(frame)) > 1e-4 for frame, _ in samples):
        raise ValueError("Move manual pose keys to whole frames before retargeting.")

    samples = sorted({(int(round(frame)), int(round(target))) for frame, target in samples})
    user_motion_samples = []
    with context.temp_override(scene=context.scene, object=armature, active_object=armature):
        animation_data = armature.animation_data
        previous_action, previous_slot = animation_data.action, animation_data.action_slot
        try:
            animation_data.action = working_action
            animation_data.action_slot = working_slot
            for frame, target_frame in samples:
                user_motion, _canonical_motion = constraint_capture.user_pose_to_canonical_motion(
                    context,
                    armature,
                    [frame],
                )
                user_motion_samples.append((target_frame, user_motion))
        finally:
            animation_data.action = previous_action
            animation_data.action_slot = previous_slot
            context.view_layer.update()
    return spacetime_constraints.pose_constraints_from_samples(
        target_hierarchy,
        user_motion_samples,
    )
