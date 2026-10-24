"""Timeline hover, mouse input, and native Clip drag."""

import math

import bpy
from bpy.types import Operator

from ..animation import blender_actions
from ..core import takes, tracks
from .drawing import ClipDrawing
from .properties import edit_track
from .runtime import (
    HoverTarget, MenuTarget, in_timeline, runtime, select_mouse_button, tag_redraw,
)


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


def _select_clip_keys(data, clip):
    """Select only the active Take's visible keys and return one key as an anchor."""
    _clear_working_key_selection(data)
    action, visible_curves = _visible_working_curves(data, clip)
    if action is None:
        return None

    anchor = None
    for curve in visible_curves:
        for key in curve.keyframe_points:
            if clip.start <= key.co.x < clip.end:
                key.select_control_point = True
                key.select_left_handle = True
                key.select_right_handle = True
                if anchor is None:
                    anchor = key, float(key.co.x)
    return action, anchor


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
    """Detect which clip handle or sync dot is under the mouse."""
    bl_idname = "clips.hover"
    bl_label = "Clips Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return in_timeline(context)

    def invoke(self, context, event):
        data = getattr(context.scene, "clip_data", None)
        hover = HoverTarget()
        if data is not None and len(data.clips):
            view, metrics = ClipDrawing.metrics(context.region)
            hit = ClipDrawing.hit_test(
                data.clips, view, metrics, event.mouse_region_x, event.mouse_region_y,
            )
            if hit.kind in {'LEFT', 'RIGHT', 'SYNC'}:
                hover = HoverTarget(
                    hit.kind, hit.clip.uid, hit.other.uid if hit.other is not None else 0,
                )
        if hover != runtime.hover:
            runtime.hover = hover
            context.area.tag_redraw()
        return {'PASS_THROUGH'}


class CLIPS_OT_mouse(Operator):
    """Select, move, resize, and open menus for Clips."""
    bl_idname = "clips.mouse"
    bl_label = "Clips"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return in_timeline(context)

    def invoke(self, context, event):
        select_button = select_mouse_button(context)
        data = getattr(context.scene, "clip_data", None)
        if data is None:
            return {'PASS_THROUGH'}
        view, metrics = ClipDrawing.metrics(context.region)
        hit = ClipDrawing.hit_test(
            data.clips, view, metrics,
            event.mouse_region_x, event.mouse_region_y,
        )
        menu_button = 'W' if select_button == 'RIGHTMOUSE' else 'RIGHTMOUSE'
        menu_event = event.type == menu_button and event.value == 'PRESS'
        select_event = event.type == select_button and event.value in {'PRESS', 'DOUBLE_CLICK'}
        if not (menu_event or select_event):
            return {'PASS_THROUGH'}
        if hit.kind == 'NONE':
            if select_event:
                data.active_uid = 0
                tag_redraw()
            return {'PASS_THROUGH'}
        if menu_event:
            return self._open_menu(hit)
        if event.value == 'DOUBLE_CLICK':
            if hit.kind == 'EMPTY':
                return self._create_with_prompt(context, hit)
            if hit.kind == 'BODY':
                data.active_uid = hit.clip.uid
                _select_clip_keys(data, hit.clip)
                bpy.ops.clips.edit_prompt('INVOKE_DEFAULT', clip_uid=hit.clip.uid)
                return {'FINISHED'}
            return {'PASS_THROUGH'}
        if hit.kind == 'EMPTY':
            data.active_uid = 0
            tag_redraw()
            return {'PASS_THROUGH'}
        return self._begin_drag(context, event, data, hit, view)

    def _open_menu(self, hit):
        uid = 0 if hit.kind == 'EMPTY' else hit.clip.uid
        runtime.menu_target = MenuTarget(hit.frame, uid)
        bpy.ops.wm.call_menu(name="CLIPS_MT_empty" if not uid else "CLIPS_MT_clip")
        return {'CANCELLED'}

    def _create_with_prompt(self, context, hit):
        data = context.scene.clip_data
        clip = edit_track(data, tracks.create_clip_at, hit.frame, data.default_length)
        if clip is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        tag_redraw()
        bpy.ops.clips.edit_prompt('INVOKE_DEFAULT', clip_uid=clip.uid)
        return {'FINISHED'}

    def _begin_drag(self, context, event, data, hit, view):
        self._session = edit_track(
            data, tracks.begin_drag, hit.kind, hit.clip.uid,
            hit.other.uid if hit.other is not None else 0,
        )
        self._scene = context.scene
        self._mouse_button = event.type
        self._start_mouse_x = event.mouse_region_x
        self._pixels_per_frame = view.pixels_per_frame
        self._native_drag = False
        self._animation_snap_settings = None
        try:
            selected = _select_clip_keys(data, hit.clip)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if self._session.kind == 'BODY' and selected is not None:
            try:
                self._start_native_drag(context, selected)
            except ValueError as exc:
                self.report({'WARNING'}, str(exc))
                return {'CANCELLED'}
        runtime.hover = HoverTarget(hit.kind, self._session.uid, self._session.other_uid)
        context.window.cursor_modal_set('MOVE_X')
        context.window_manager.modal_handler_add(self)
        tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            data = getattr(self._scene, "clip_data", None)
            clip = tracks.find_clip(data.clips, self._session.uid) if data is not None else None
        except ReferenceError:
            clip = None
        if clip is None or context.scene != self._scene:
            self.cancel(context)
            return {'CANCELLED'}
        if self._native_drag:
            return self._modal_native_drag(context, data, event)
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            delta = round((event.mouse_region_x - self._start_mouse_x) / self._pixels_per_frame)
            edit_track(data, tracks.update_drag, self._session, delta)
            tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == self._mouse_button and event.value == 'RELEASE':
            self._finish(context)
            return {'FINISHED'}
        if event.value == 'PRESS' and (
            event.type == 'ESC'
            or event.type in {'LEFTMOUSE', 'RIGHTMOUSE'} and event.type != self._mouse_button
        ):
            self.cancel(context)
            return {'CANCELLED'}
        if event.type in {
            'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
        }:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}

    def _start_native_drag(self, context, selected):
        """Start Blender's native key transform for the active Take."""
        _action, anchor = selected
        if anchor is None:
            return

        tool_settings = context.scene.tool_settings
        self._animation_snap_settings = (
            tool_settings,
            tool_settings.snap_anim_element,
            tool_settings.use_snap_time_absolute,
        )
        tool_settings.snap_anim_element = 'FRAME'
        tool_settings.use_snap_time_absolute = False
        try:
            result = bpy.ops.transform.transform(
                'INVOKE_DEFAULT',
                mode='TIME_TRANSLATE',
                use_proportional_edit=False,
                snap=True,
                release_confirm=True,
            )
        except Exception as exc:
            self._restore_animation_snapping()
            raise ValueError(f"Could not start native Time Translate: {exc}") from None
        if 'RUNNING_MODAL' not in result:
            self._restore_animation_snapping()
            raise ValueError("Native Time Translate did not start a modal operation.")

        self._native_drag = True

    def _restore_animation_snapping(self):
        settings = self._animation_snap_settings
        if settings is None:
            return
        tool_settings, snap_anim_element, use_snap_time_absolute = settings
        tool_settings.snap_anim_element = snap_anim_element
        tool_settings.use_snap_time_absolute = use_snap_time_absolute
        self._animation_snap_settings = None

    def _modal_native_drag(self, context, data, event):
        """Pass a mouse move to TIME_TRANSLATE only when it fits."""
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            delta = math.floor(
                (event.mouse_region_x - self._start_mouse_x)
                / self._pixels_per_frame + 0.5
            )
            edit_track(data, tracks.update_drag, self._session, delta)
            tag_redraw()
            if self._session.last_valid_delta == delta:
                return {'PASS_THROUGH'}
            return {'RUNNING_MODAL'}
        if event.type == self._mouse_button and event.value == 'RELEASE':
            self._restore_animation_snapping()
            self._native_drag = False
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if event.value == 'PRESS' and (
            event.type == 'ESC'
            or event.type in {'LEFTMOUSE', 'RIGHTMOUSE'} and event.type != self._mouse_button
        ):
            edit_track(data, tracks.cancel_drag, self._session)
            self._restore_animation_snapping()
            self._native_drag = False
            self._finish(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        return {'RUNNING_MODAL', 'PASS_THROUGH'}

    def cancel(self, context):
        if self._native_drag:
            try:
                data = getattr(self._scene, "clip_data", None)
                if data is not None:
                    edit_track(data, tracks.cancel_drag, self._session)
            except ReferenceError:
                pass
            self._restore_animation_snapping()
            self._native_drag = False
            self._finish(context)
            return
        try:
            data = getattr(self._scene, "clip_data", None)
            if data is not None:
                edit_track(data, tracks.cancel_drag, self._session)
        except ReferenceError:
            pass
        finally:
            self._finish(context)

    def _finish(self, context):
        context.window.cursor_modal_restore()
        runtime.hover = HoverTarget()
        tag_redraw()
