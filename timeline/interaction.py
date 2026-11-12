"""Timeline hover, mouse input, and Clip drag."""

import math
from dataclasses import replace
from time import monotonic

import bpy
from bpy.types import Operator

from ..animation import blender_actions
from ..core import takes, tracks
from .drawing import ClipDrawing
from . import operations
from .properties import edit_track, ensure_track, find_track
from .runtime import (
    HoverHint, HoverTarget, MenuTarget, has_selected_armature, in_timeline, runtime, tag_redraw,
)
from . import actions
from .track_style import STYLE


def _visible_working_curves(data, clip):
    """Return Working-Action curves corresponding to the active Take."""
    active_take = takes.current_take(clip)
    if active_take is None:
        return None, ()

    working_action = data.working_action
    working_slot = blender_actions.find_action_slot_by_handle(
        working_action,
        int(data.working_slot_handle),
    )
    if working_slot is None:
        raise ValueError("Working Action binding is incomplete.")

    take_action = active_take.take_action
    take_slot = blender_actions.find_action_slot_by_handle(
        take_action,
        int(active_take.take_slot_handle),
    )
    if take_slot is None:
        raise ValueError(f"Take {active_take.uid} Action binding is incomplete.")

    working = {
        (curve.data_path, int(curve.array_index)): curve
        for curve in blender_actions.iter_slot_fcurves(working_action, working_slot)
    }
    curves = []
    for take_curve in blender_actions.iter_slot_fcurves(take_action, take_slot):
        identity = (take_curve.data_path, int(take_curve.array_index))
        curve = working.get(identity)
        if curve is None:
            raise ValueError(f"Working Action has no curve for {identity!r}.")
        curves.append(curve)
    return working_action, curves


def _clear_selected_armature_key_selection(context):
    """Deselect keys in every selected armature Action through Blender's operator."""
    armatures = []
    seen_actions = set()
    for armature in context.selected_objects:
        if armature.type != 'ARMATURE':
            continue
        animation_data = armature.animation_data
        if animation_data is None or animation_data.action is None:
            continue
        action = animation_data.action
        if action in seen_actions:
            continue
        seen_actions.add(action)
        armatures.append(armature)

    for armature in armatures:
        try:
            with context.temp_override(active_object=armature, object=armature):
                bpy.ops.action.select_all(action='DESELECT')
        except RuntimeError as exc:
            raise ValueError("Could not clear selected animation keys.") from exc


def _select_all_pose_bones(armature):
    """Select all pose bones and save the user's previous selection."""
    bones = armature.pose.bones
    selected = {bone.name: bool(bone.select) for bone in bones}

    active_bone = armature.data.bones.active
    active = active_bone.name if active_bone is not None else None

    for bone in bones:
        bone.select = True

    return selected, active


def _restore_pose_bone_selection(armature, selection):
    """Restore the user's bone selection after Clip drag."""
    if selection is None:
        return

    selected, active_name = selection
    bones = armature.pose.bones

    for bone in bones:
        bone.select = selected.get(bone.name, False)

    armature.data.bones.active = (
        armature.data.bones.get(active_name)
        if active_name is not None else None
    )


def _select_clip_keys(context, data, clip, *, clear_selection):
    """Select every Working-Action Slot key in the Clip range."""
    if clear_selection:
        _clear_selected_armature_key_selection(context)
    action = data.working_action
    slot = blender_actions.find_action_slot_by_handle(action, int(data.working_slot_handle))
    if slot is None:
        raise ValueError("Working Action binding is incomplete.")

    for curve in blender_actions.iter_slot_fcurves(action, slot):
        points = curve.keyframe_points

        left = actions._lower_bound(points, clip.start)
        right = actions._lower_bound(points, clip.end)

        for i in range(left, right):
            key = points[i]

            key.select_control_point = True
            key.select_left_handle = True
            key.select_right_handle = True


def _clear_working_key_selection(data):
    action = data.working_action
    slot = blender_actions.find_action_slot_by_handle(action, int(data.working_slot_handle))
    if slot is None:
        return
    for curve in blender_actions.iter_slot_fcurves(action, slot):
        for key in curve.keyframe_points:
            key.select_control_point = False
            key.select_left_handle = False
            key.select_right_handle = False


class CLIPS_OT_hover(Operator):
    """Hover over a clip to see its name, take count, and duration guidance"""
    bl_idname = "clips.hover"
    bl_label = "Clips Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return in_timeline(context) and has_selected_armature(context)

    def invoke(self, context, event):
        if runtime.hover_timer is not None:
            return {'PASS_THROUGH'}
        self._hover_started = monotonic()
        if not self._update_hover(context, event):
            return {'PASS_THROUGH'}
        self._timer = context.window_manager.event_timer_add(
            STYLE.hover_timer_interval, window=context.window,
        )
        runtime.hover_timer = self._timer
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL', 'PASS_THROUGH'}

    def _update_hover(self, context, event):
        if not self.poll(context):
            return False
        region = context.region
        mouse_x, mouse_y = event.mouse_x - region.x, event.mouse_y - region.y
        if not (0 <= mouse_x < region.width and 0 <= mouse_y < region.height):
            return False
        track = find_track(context)
        view, metrics = ClipDrawing.metrics(region)
        hit = ClipDrawing.hit_test(
            () if track is None else track.clips, view, metrics, mouse_x, mouse_y,
        )
        if hit.kind == 'NONE':
            return False
        hover = HoverTarget(
            hit.kind, track.uid if track is not None else 0,
            hit.clip.uid if hit.clip is not None else 0,
            hit.other.uid if hit.other is not None else 0,
        )
        hint = HoverHint(region.as_pointer(), mouse_x, mouse_y)
        if hover != runtime.hover or hint != replace(runtime.hover_hint, visible=False):
            runtime.hover = hover
            runtime.hover_hint = hint
            self._hover_started = monotonic()
            context.area.tag_redraw()
        return True

    def modal(self, context, event):
        # Reset/unregister may already have removed this operator's timer.
        if runtime.hover_timer != self._timer:
            return {'FINISHED', 'PASS_THROUGH'}
        if event.type.startswith('TIMER'):
            if event.type != 'TIMER':
                return {'PASS_THROUGH'}
        elif event.type not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.cancel(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if not self._update_hover(context, event):
            self.cancel(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if (
            not runtime.hover_hint.visible
            and monotonic() - self._hover_started >= STYLE.tooltip_delay
        ):
            runtime.hover_hint = replace(runtime.hover_hint, visible=True)
            context.area.tag_redraw()
        return {'PASS_THROUGH'}

    def cancel(self, context):
        if runtime.hover_timer == self._timer:
            runtime.clear_hover()
            tag_redraw()


class CLIPS_OT_mouse(Operator):
    """Click to select, double-click to rename, drag to move, or drag an edge to resize. Right-click for more options"""
    bl_idname = "clips.mouse"
    bl_label = "Clips"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return in_timeline(context)

    def invoke(self, context, event):
        track = find_track(context)
        clips = () if track is None else track.clips
        view, metrics = ClipDrawing.metrics(context.region)
        hit = ClipDrawing.hit_test(
            clips, view, metrics,
            event.mouse_region_x, event.mouse_region_y,
        )
        menu_event = event.type == 'RIGHTMOUSE' and event.value == 'PRESS'
        select_event = event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'DOUBLE_CLICK'}
        if not (menu_event or select_event):
            return {'PASS_THROUGH'}
        if hit.kind == 'NONE':
            if select_event and track is not None:
                track.active_clip_uid = 0
                tag_redraw()
            return {'PASS_THROUGH'}
        if menu_event:
            return self._open_menu(hit, track)
        if event.value == 'DOUBLE_CLICK':
            if hit.kind == 'EMPTY':
                return self._create_with_prompt(context, hit)
            if hit.kind == 'BODY':
                track.active_clip_uid = hit.clip.uid
                _select_clip_keys(
                    context, track, hit.clip, clear_selection=not event.shift,
                )
                bpy.ops.clips.rename('INVOKE_DEFAULT', clip_uid=hit.clip.uid)
                return {'FINISHED'}
            return {'PASS_THROUGH'}
        if hit.kind == 'EMPTY':
            if track is not None:
                track.active_clip_uid = 0
                tag_redraw()
            return {'PASS_THROUGH'}
        return self._begin_drag(context, event, track, hit, view)

    def _open_menu(self, hit, track):
        uid = 0 if hit.kind == 'EMPTY' else hit.clip.uid
        runtime.menu_target = MenuTarget(hit.frame, 0 if track is None else track.uid, uid)
        bpy.ops.wm.call_menu(name="CLIPS_MT_empty" if not uid else "CLIPS_MT_clip")
        return {'CANCELLED'}

    def _create_with_prompt(self, context, hit):
        scene = context.scene
        track = find_track(context)
        clips = () if track is None else track.clips
        if tracks.new_range(clips, hit.frame, scene.clip_data.default_length) is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        try:
            track = track or ensure_track(context)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        clip = operations.create_clip(
            track, hit.frame, scene.clip_data.default_length, stick_previous=True,
        )
        if clip is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        tag_redraw()
        bpy.ops.clips.edit_prompt('INVOKE_DEFAULT', clip_uid=clip.uid)
        return {'FINISHED'}

    def _begin_drag(self, context, event, track, hit, view):
        runtime.clear_hover()
        self._session = edit_track(
            track, tracks.begin_drag, hit.kind, hit.clip.uid,
            hit.other.uid if hit.other is not None else 0,
        )
        self._scene = context.scene
        self._track = track
        self._start_mouse_x = event.mouse_region_x
        self._pixels_per_frame = view.pixels_per_frame
        self._key_translations = []
        self._bone_selection = None
        if self._session.kind == 'BODY':
            self._bone_selection = _select_all_pose_bones(track.object)
        try:
            _select_clip_keys(
                context, track, hit.clip, clear_selection=not event.shift,
            )
            if self._session.kind == 'BODY':
                seen = set()
                for armature in context.selected_objects:
                    animation = armature.animation_data
                    if armature.type != 'ARMATURE' or animation is None:
                        continue
                    action, slot = animation.action, animation.action_slot
                    if slot is None or (action, slot.handle) in seen:
                        continue
                    seen.add((action, slot.handle))
                    self._key_translations.append((action, actions.snapshot_key_translation(
                        blender_actions.iter_slot_fcurves(action, slot),
                    )))
        except ValueError as exc:
            _restore_pose_bone_selection(track.object, self._bone_selection)
            self._bone_selection = None
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        runtime.hover = HoverTarget(hit.kind, track.uid, self._session.uid, self._session.other_uid)
        context.window.cursor_modal_set('MOVE_X')
        context.window_manager.modal_handler_add(self)
        tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            track = self._track
            clip = tracks.find_clip(track.clips, self._session.uid)
        except ReferenceError:
            clip = None
        if clip is None or context.scene != self._scene:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type == 'MOUSEMOVE' or (
            self._session.kind != 'BODY' and event.type == 'INBETWEEN_MOUSEMOVE'
        ):
            movement = (event.mouse_region_x - self._start_mouse_x) / self._pixels_per_frame
            delta = math.floor(movement + 0.5) if self._session.kind == 'BODY' else round(movement)
            previous_delta = self._session.last_valid_delta
            edit_track(track, tracks.update_drag, self._session, delta)
            if self._session.kind == 'BODY' and self._session.last_valid_delta != previous_delta:
                self._translate_keys(self._session.last_valid_delta)
            tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if not self._finish_resize_drag():
                self._finish(context)
                return {'CANCELLED'}
            for action, snapshot in self._key_translations:
                actions.finish_key_translation(snapshot)
                action.update_tag()
            self._finish(context)
            return {'FINISHED'}
        if event.value == 'PRESS' and event.type in {'ESC', 'RIGHTMOUSE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type in {
            'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
        }:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}

    def _translate_keys(self, delta):
        for action, snapshot in self._key_translations:
            actions.apply_key_translation(snapshot, delta)
            action.update_tag()

    def _finish_resize_drag(self):
        try:
            operations.finalize_resize_drag(self._track, self._session)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not apply Clip resize: {exc}")
            return False
        return True

    def cancel(self, context):
        try:
            edit_track(self._track, tracks.cancel_drag, self._session)
            self._translate_keys(0)
        except ReferenceError:
            pass
        finally:
            self._finish(context)

    def _finish(self, context):
        self._key_translations = []
        _restore_pose_bone_selection(self._track.object, self._bone_selection)
        self._bone_selection = None
        context.window.cursor_modal_restore()
        runtime.clear_hover()
        tag_redraw()
