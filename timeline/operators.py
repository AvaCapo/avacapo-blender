"""Timeline commands, generation dialogs, and user-facing reports."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

from ..animation import test_take_generation
from ..common.config import Config
from ..core import takes, tracks
from ..core.generation_models import GenerationCommand, TextParameters
from ..server import models
from . import operations
from .properties import edit_track, ensure_track, find_track
from .runtime import runtime, tag_redraw
from .track_style import STYLE


class _ClipOp:
    """Resolve a clip UID (0 means the selected clip) for Blender operators."""
    clip_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _get_clip(self, context):
        track = find_track(context)
        if track is None:
            self.report({'WARNING'}, "No Track for the current Action and Slot")
            return None, None
        clip = tracks.find_clip(track.clips, self.clip_uid or track.active_clip_uid)
        if clip is None:
            self.report({'WARNING'}, "No clip")
        return track, clip

    def _generate_take(self, context, track, clip, *, command=None):
        if command is None:
            command = GenerationCommand(
                prompt=clip.prompt,
                model=clip.generation_model,
                in_place=clip.generation_in_place,
                parameters=TextParameters(
                    duration=(clip.end - clip.start) / Config.DEFAULT_FPS,
                    fps=Config.DEFAULT_FPS,
                    temperature=clip.generation_temperature,
                    cycled=clip.generation_cycled,
                    seed=clip.seed,
                ),
            )
        try:
            # TODO(server-jobs): re-enable operations.submit_generation() when
            # the production server supports the server-jobs protocol: operations.submit_generation(...)
            
            # Generation through server jobs:
            # operations.submit_generation(
            #     context.scene,
            #     track,
            #     clip,
            #     command,
            #     scene_fps=float(context.scene.render.fps) / float(context.scene.render.fps_base),
            # )
            
            #NOTE: use file read for fast prototyping
            # Generation from the bundled test BVH:
            # source_motion, source_text = test_take_generation.load_test_motion()
            # operations.apply_generated_motion(
            #     track,
            #     clip,
            #     command=command,
            #     source_motion=source_motion,
            #     source_text=source_text,
            #     scene_fps=float(context.scene.render.fps) / float(context.scene.render.fps_base),
            # )

            # Direct production POST -> BVH:
            test_take_generation.start_direct_generation(context, track, clip, command)
        except (OSError, RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not generate Take: {exc}")
            return False
        self.report({'INFO'}, "Production generation started")
        return True


class CLIPS_OT_create(Operator):
    """Create a clip: default length, shortened to fit before the next clip"""
    bl_idname = "clips.create"
    bl_label = "Create Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        track = find_track(context)
        clips = () if track is None else track.clips
        if tracks.new_range(clips, self.frame, scene.clip_data.default_length) is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        try:
            track = track or ensure_track(context)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if operations.create_clip(track, self.frame, scene.clip_data.default_length) is None:
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
        track = find_track(context)
        clips = () if track is None else track.clips
        if not tracks.fill_ranges(
            clips, self.frame, scene.clip_data.default_length, scene.frame_start, scene.frame_end,
        ):
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        try:
            track = track or ensure_track(context)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not edit_track(
            track, tracks.fill_gap, self.frame, scene.clip_data.default_length,
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
        track = find_track(context)
        if track is None:
            self.report({'INFO'}, "No Track for the current Action and Slot")
            return {'CANCELLED'}
        if not edit_track(
            track, tracks.shrink_neighbors, self.frame, scene.frame_current,
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
    seed: IntProperty(name="Seed", min=-1, default=-1, options={'SKIP_SAVE'})
    model: EnumProperty(
        name="Model", description="Generation model", items=models.get_model_enum_items,
        options={'SKIP_SAVE'},
    )
    temperature: FloatProperty(name="Temperature", min=0.001, options={'SKIP_SAVE'})
    cycled: BoolProperty(name="Cycled", options={'SKIP_SAVE'})
    in_place: BoolProperty(name="In Place", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        _, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        self.prompt = clip.prompt
        self.seed = clip.seed
        self.model = clip.generation_model
        self.temperature = clip.generation_temperature
        self.cycled = clip.generation_cycled
        self.in_place = clip.generation_in_place
        return context.window_manager.invoke_props_dialog(self, width=STYLE.prompt_dialog_width)

    def draw(self, context):
        col = self.layout.column()
        row = col.row()
        row.activate_init = True
        row.prop(self, "prompt", text="")
        col.prop(self, "seed")
        row = col.row(align=True)
        row.prop(self, "model")
        row.prop(self, "temperature")
        row = col.row(align=True)
        row.prop(self, "cycled")
        row.prop(self, "in_place")

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.prompt = self.prompt
        clip.seed = self.seed
        clip.generation_model = self.model
        clip.generation_temperature = self.temperature
        clip.generation_cycled = self.cycled
        clip.generation_in_place = self.in_place
        if not self._generate_take(
            context, track, clip,
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
                    seed=self.seed,
                ),
            ),
        ):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_new_take(_ClipOp, Operator):
    """Generate a new Take from this Clip's editable generation defaults."""
    bl_idname = "clips.new_take"
    bl_label = "New Take"
    bl_description = ""
    bl_options = {'UNDO'}

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not self._generate_take(context, track, clip):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_takes(_ClipOp, Operator):
    """Delete all takes of the clip"""
    bl_idname = "clips.delete_takes"
    bl_label = "Delete All Takes"
    bl_options = {'UNDO'}

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        try:
            operations.delete_takes(track, clip)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not delete Takes: {exc}")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_take(_ClipOp, Operator):
    """Delete one Take from this Clip."""
    bl_idname = "clips.delete_take"
    bl_label = "Delete Take"
    bl_description = "Delete this Take from the Clip"
    bl_options = {'UNDO'}
    take_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        try:
            operations.delete_take(track, clip, self.take_uid)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not delete Take: {exc}")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_clip(_ClipOp, Operator):
    """Delete the clip"""
    bl_idname = "clips.delete_clip"
    bl_label = "Delete Clip"
    bl_options = {'UNDO'}

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        try:
            operations.delete_clip(track, clip)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not delete Clip: {exc}")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_bake_clip(_ClipOp, Operator):
    """Keep visible animation and remove the Clip and its Takes."""
    bl_idname = "clips.bake_clip"
    bl_label = "Bake Clip"
    bl_options = {'UNDO'}

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        try:
            operations.bake_clip(track, clip)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not bake Clip: {exc}")
            return {'CANCELLED'}
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
        try:
            runtime.set_clipboard(operations.copy_clip(clip))
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not copy Clip: {exc}")
            return {'CANCELLED'}
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
        track = find_track(context)
        try:
            track = track or ensure_track(context)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        try:
            operations.paste_clip(track, runtime.clipboard, self.frame)
        except (RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not paste Clip: {exc}")
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
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        try:
            take = operations.switch_take(track, clip, self.take_uid)
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
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not edit_track(track, tracks.stick_to_neighbors, clip.uid, self.mode):
            self.report({'INFO'}, "There is no neighbour to stick to")
            return {'CANCELLED'}
        if not self._generate_take(context, track, clip):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_set_frame(_ClipOp, Operator):
    """Set the clip border to the current frame"""
    bl_idname = "clips.set_frame"
    bl_label = "Set to Current Frame"
    bl_options = {'UNDO'}
    which: EnumProperty(
        items=[('START', "Start", ""), ('END', "End", "")], options={'HIDDEN', 'SKIP_SAVE'},
    )

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if self.which == 'START':
            operations.move_clip(track, clip.uid, context.scene.frame_current)
        else:
            operations.resize_clip(
                track, clip.uid, tracks.resize_end, context.scene.frame_current,
            )
        tag_redraw()
        return {'FINISHED'}
