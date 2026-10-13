"""Blender events and operators; timeline mutations are delegated to core."""

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

from ..core import takes, tracks
from .track_drawing import ClipDrawing
from .track_generation import request_generation
from .track_properties import edit_track
from .track_runtime import (
    HoverTarget, MenuTarget, in_timeline, runtime, select_mouse_button, tag_redraw,
)
from .track_style import STYLE


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
        data = getattr(context.scene, "clip_data", None)
        if data is None:
            return {'PASS_THROUGH'}
        view, metrics = ClipDrawing.metrics(context.region)
        hit = ClipDrawing.hit_test(
            data.clips, view, metrics, event.mouse_region_x, event.mouse_region_y,
        )
        if hit.kind == 'NONE':
            return {'PASS_THROUGH'}
        select_button = select_mouse_button(context)
        if event.type == 'W':
            return self._open_menu(hit) if select_button == 'RIGHTMOUSE' else {'PASS_THROUGH'}
        if event.type != select_button:
            if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
                return self._open_menu(hit)
            return {'PASS_THROUGH'}
        if hit.kind == 'EMPTY':
            if event.value == 'DOUBLE_CLICK':
                return self._create_with_prompt(context, hit)
            if data.active_uid:
                data.active_uid = 0
                tag_redraw()
                return {'FINISHED'}
            return {'CANCELLED'}
        if event.value == 'DOUBLE_CLICK':
            if hit.kind == 'BODY':
                data.active_uid = hit.clip.uid
                bpy.ops.clips.edit_prompt(
                    'INVOKE_DEFAULT', clip_uid=hit.clip.uid, make_take=False,
                )
                return {'FINISHED'}
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
        bpy.ops.clips.edit_prompt('INVOKE_DEFAULT', clip_uid=clip.uid, make_take=True)
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

    def cancel(self, context):
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

    def _generate_take(self, context, clip):
        take = edit_track(context.scene.clip_data, takes.add_take, clip)
        try:
            request_generation(context.scene, clip.uid, take.uid)
        except (RuntimeError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not start generation: {exc}")


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
    """Edit the prompt and settings of a clip"""
    bl_idname = "clips.edit_prompt"
    bl_label = "Clip Prompt"
    bl_options = {'UNDO'}
    prompt: StringProperty(name="Prompt", options={'SKIP_SAVE'})
    seed: IntProperty(name="Seed", min=0, options={'SKIP_SAVE'}, description="0 = random")
    make_take: BoolProperty(name="Generate New Take", default=True, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        self.prompt, self.seed = clip.prompt, clip.seed
        return context.window_manager.invoke_props_dialog(self, width=STYLE.prompt_dialog_width)

    def draw(self, context):
        col = self.layout.column()
        row = col.row()
        row.activate_init = True
        row.prop(self, "prompt", text="")
        col.prop(self, "seed")
        col.prop(self, "make_take")

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.prompt, clip.seed = self.prompt, self.seed
        if self.make_take:
            self._generate_take(context, clip)
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_new_take(_ClipOp, Operator):
    """Generate a new take with the clip's current prompt and settings"""
    bl_idname = "clips.new_take"
    bl_label = "New Take"
    bl_options = {'UNDO'}

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        self._generate_take(context, clip)
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
    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not takes.select_take(clip, self.index):
            self.report({'WARNING'}, "No take at this index")
            return {'CANCELLED'}
        tag_redraw()
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
        self._generate_take(context, clip)
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
