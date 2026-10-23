"""Blender events and operators; timeline mutations are delegated to core."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

from ..common.config import Config
from ..animation import blender_actions, test_take_generation
from ..core import takes, tracks
from ..core.generation_models import GenerationCommand, TextParameters
from .track_drawing import ClipDrawing
from .track_properties import edit_track
from .track_runtime import (
    HoverTarget, MenuTarget, in_timeline, runtime, select_mouse_button, tag_redraw,
)
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
    """Detect which clip handle or sync dot is under the mouse"""
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
    """Select / move / resize clips, open menus, create clips"""
    bl_idname = "clips.mouse"
    bl_label = "Clips"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return in_timeline(context)

    def invoke(self, context, event):
        select_button = select_mouse_button(context)
        data = getattr(context.scene, "clip_data", None)
        view, metrics = ClipDrawing.metrics(context.region)
        hit = ClipDrawing.hit_test(
            data.clips, view, metrics,
            event.mouse_region_x, event.mouse_region_y,
        )
        menu_button = 'W' if select_button == 'RIGHTMOUSE' else 'RIGHTMOUSE'
        menu_event = (
            event.type == menu_button
            and event.value == 'PRESS'
        )
        select_event = (
            event.type == select_button
            and event.value in {'PRESS', 'DOUBLE_CLICK'}
        )
        if not (menu_event or select_event):
            return {'PASS_THROUGH'}
        if data is None:
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
        """Start Blender's native key transform for the active Take.

        TODO(native-time-translate): TIME_TRANSLATE exposes no public API for
        neighbour bounds. This experiment deliberately runs it even for a Clip
        with neighbours, so its mismatch with the retained Clip bounds can be
        observed in the UI.
        """
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
                release_confirm=True
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
        """Experiment: pass a mouse move to TIME_TRANSLATE only when it fits."""
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


class _ClipOp:
    """Resolve a clip UID (0 means the selected clip) for Blender operators."""
    clip_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _get_clip(self, context):
        data = context.scene.clip_data
        clip = tracks.find_clip(data.clips, self.clip_uid or data.active_uid)
        if clip is None:
            self.report({'WARNING'}, "No clip")
        return data, clip

    def _generate_take(self, context, clip, *, command=None):
        # TODO(test-generation): replace the bundled BVH result with the server
        # response, preserving the same Take-Action materialize contract.
        try:
            take = test_take_generation.generate_test_take(
                context.scene,
                clip,
                create_take=lambda: edit_track(
                    context.scene.clip_data,
                    takes.add_take,
                    clip,
                    command=command,
                    activate=False,
                ),
            )
        except (OSError, RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not generate Take: {exc}")
            return False
        self.report({'INFO'}, f"Generated {take.name} from the temporary test BVH")
        return True


class CLIPS_OT_create(Operator):
    """Create a clip: default length, shortened to fit before the next clip"""
    bl_idname = "clips.create"
    bl_label = "Create Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        data = context.scene.clip_data
        if edit_track(data, tracks.create_clip_at, self.frame, data.default_length) is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_fill_gap(Operator):
    """Fill the empty space under the cursor with clips of about the default length"""
    bl_idname = "clips.fill_gap"
    bl_label = "Fill In"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        data = scene.clip_data
        if not edit_track(
            data, tracks.fill_gap, self.frame, data.default_length,
            scene.frame_start, scene.frame_end,
        ):
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_shrink_neighbors(Operator):
    """Cut the clips next to this empty space back to the current frame"""
    bl_idname = "clips.shrink_neighbors"
    bl_label = "Shrink Neighbors to Current Frame"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        if not edit_track(
            scene.clip_data, tracks.shrink_neighbors, self.frame, scene.frame_current,
        ):
            self.report({'INFO'}, "The current frame is not inside a neighbouring clip")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_edit_prompt(_ClipOp, Operator):
    """Collect generation settings and create a Take."""
    bl_idname = "clips.edit_prompt"
    bl_label = "Generate Take"
    bl_options = {'UNDO'}
    prompt: StringProperty(name="Prompt", options={'SKIP_SAVE'})
    model: StringProperty(
        name="Model", default=Config.DEFAULT_MODEL_TYPE, options={'SKIP_SAVE'},
    )
    temperature: FloatProperty(name="Temperature", min=0.001, options={'SKIP_SAVE'})
    cycled: BoolProperty(name="Cycled", options={'SKIP_SAVE'})
    in_place: BoolProperty(name="In Place", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        self.temperature = Config.DEFAULT_TEMPERATURE
        return context.window_manager.invoke_props_dialog(self, width=STYLE.prompt_dialog_width)

    def draw(self, context):
        col = self.layout.column()
        row = col.row()
        row.activate_init = True
        row.prop(self, "prompt", text="")
        row = col.row(align=True)
        row.prop(self, "model")
        row.prop(self, "temperature")
        row = col.row(align=True)
        row.prop(self, "cycled")
        row.prop(self, "in_place")

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not self._generate_take(
            context, clip,
            command=GenerationCommand(
                prompt=self.prompt, model=self.model,
                in_place=self.in_place,
                parameters=TextParameters(
                    # TODO(postprocess-fps): keep the model request at the configured
                    # 24 FPS. Scene FPS is for later local post-processing, not this request.
                    duration=(clip.end - clip.start) / Config.DEFAULT_FPS,
                    fps=Config.DEFAULT_FPS,
                    temperature=self.temperature,
                    cycled=self.cycled,
                ),
            ),
        ):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_new_take(_ClipOp, Operator):
    """Generate a new Take with empty UI settings."""
    bl_idname = "clips.new_take"
    bl_label = "New Take"
    bl_options = {'UNDO'}

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not self._generate_take(context, clip):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_takes(_ClipOp, Operator):
    """Delete all takes of the clip"""
    bl_idname = "clips.delete_takes"
    bl_label = "Delete All Takes"
    bl_options = {'UNDO'}

    def execute(self, context):
        data, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        edit_track(data, takes.delete_takes, clip)
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_clip(_ClipOp, Operator):
    """Delete the clip"""
    bl_idname = "clips.delete_clip"
    bl_label = "Delete Clip"
    bl_options = {'UNDO'}

    def execute(self, context):
        data, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        edit_track(data, tracks.remove_clip, clip.uid)
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_copy_clip(_ClipOp, Operator):
    """Copy the clip with its takes"""
    bl_idname = "clips.copy_clip"
    bl_label = "Copy Clip"
    bl_options = set()

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        # TODO: copy the Clip-owned Text records together with metadata.
        # Copying only RNA creates Takes whose source/payload is missing.
        runtime.clipboard = tracks.serialize_clip(clip)
        return {'FINISHED'}


class CLIPS_OT_paste_clip(Operator):
    """Paste the copied clip here if there is enough free space"""
    bl_idname = "clips.paste_clip"
    bl_label = "Paste Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        if runtime.clipboard is None:
            return {'CANCELLED'}
        # TODO: restore independent Text records before exposing pasted Takes.
        clip = edit_track(context.scene.clip_data, tracks.paste_clip, runtime.clipboard, self.frame)
        if clip is None:
            self.report({'WARNING'}, "Not enough free space for the copied clip")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_set_take(_ClipOp, Operator):
    """Make this take the visible one"""
    bl_idname = "clips.set_take"
    bl_label = "Choose Take"
    bl_options = {'UNDO'}
    take_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        try:
            take = test_take_generation.switch_take(context.scene, clip, self.take_uid)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not switch Take: {exc}")
            return {'CANCELLED'}
        tag_redraw()
        self.report({'INFO'}, f"Switched to {take.name}")
        return {'FINISHED'}


class CLIPS_OT_stick(_ClipOp, Operator):
    """Stretch the clip to its neighbours and generate a new take"""
    bl_idname = "clips.stick"
    bl_label = "Stick to Neighbors"
    bl_options = {'UNDO'}
    mode: EnumProperty(
        items=[('BOTH', "Both", ""), ('NEXT', "Next", ""), ('PREV', "Previous", "")],
        options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        data, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not edit_track(data, tracks.stick_to_neighbors, clip.uid, self.mode):
            self.report({'INFO'}, "There is no neighbour to stick to")
            return {'CANCELLED'}
        if not self._generate_take(context, clip):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_set_frame(_ClipOp, Operator):
    """Set the clip border to the current frame"""
    bl_idname = "clips.set_frame"
    bl_label = "Set to Current Frame"
    bl_options = {'UNDO'}
    which: EnumProperty(
        items=[('START', "Start", ""), ('END', "End", "")], options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        data, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        operation = tracks.resize_start if self.which == 'START' else tracks.resize_end
        edit_track(data, operation, clip.uid, context.scene.frame_current)
        tag_redraw()
        return {'FINISHED'}
