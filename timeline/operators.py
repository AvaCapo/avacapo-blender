"""Timeline commands, generation dialogs, and user-facing reports."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

from ..common.config import Config, TimelineConfig
from ..core import takes, tracks
from ..core.generation_models import GenerationCommand, TextParameters
from ..server import models
from . import operations
from .generation_status import take_description
from .properties import edit_track, ensure_track, find_track
from .runtime import has_selected_armature, runtime, tag_redraw
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
        scene_fps = float(context.scene.render.fps) / float(context.scene.render.fps_base)
        if command is None:
            prompt = clip.prompt.strip() or clip.name.strip()
            if not prompt:
                self.report({'WARNING'}, "Enter a prompt in Clip Settings or an override.")
                return False
            command = GenerationCommand(
                prompt=prompt,
                model=clip.generation_model,
                in_place=clip.generation_in_place,
                enhance_prompt=clip.generation_enhance_prompt,
                parameters=TextParameters(
                    # Clip duration is measured in scene seconds.
                    duration=(clip.end - clip.start) / scene_fps,
                    # TODO(postprocess-fps): keep requests at the configured 24 FPS;
                    # convert the returned motion to scene FPS locally.
                    fps=Config.DEFAULT_FPS,
                    temperature=clip.generation_temperature,
                    cycled=clip.generation_cycled,
                    seed=clip.seed,
                ),
            )
        try:
            operations.submit_generation(
                context.scene,
                track,
                clip,
                command,
                scene_fps=scene_fps,
            )
        except (OSError, RuntimeError, ValueError, ReferenceError) as exc:
            self.report({'WARNING'}, f"Could not generate Take: {exc}")
            return False
        self.report({'INFO'}, "Generation queued")
        return True


class _ClipCreation:
    """Collect a prompt before either creation operator mutates the timeline."""
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    prompt: StringProperty(
        name="Prompt", description="Describe the motion; this also names the new clips",
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        return has_selected_armature(context)

    def invoke(self, context, event):
        self.prompt = ""
        return context.window_manager.invoke_props_dialog(self, width=STYLE.prompt_dialog_width)

    def draw(self, context):
        row = self.layout.row()
        row.activate_init = True
        row.prop(self, "prompt", text="", placeholder="prompt")

    def _prompt(self):
        prompt = self.prompt.strip()
        if not prompt:
            self.report({'WARNING'}, "Enter a prompt for the clip.")
        return prompt


class CLIPS_OT_create(_ClipCreation, Operator):
    """Create a clip for your motion prompt. Then click Generate Take to animate it"""
    bl_idname = "clips.create"
    bl_label = "Create Clip"
    bl_options = {'UNDO'}
    stick_previous: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        prompt = self._prompt()
        if not prompt:
            return {'CANCELLED'}
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
        clip = operations.create_clip(
            track, self.frame, scene.clip_data.default_length,
            prompt=prompt, stick_previous=self.stick_previous,
        )
        if clip is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        scene.clip_data.new_clip_prompt = ""
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_fill_gap(_ClipCreation, Operator):
    """Fill the empty space with clips sharing one prompt. Select each clip to generate its animation"""
    bl_idname = "clips.fill_gap"
    bl_label = "Fill In"
    bl_options = {'UNDO'}

    def execute(self, context):
        scene = context.scene
        prompt = self._prompt()
        if not prompt:
            return {'CANCELLED'}
        scene_fps = scene.render.fps / scene.render.fps_base
        max_length = max(1, math.floor(TimelineConfig.RECOMMENDED_MAX_SECONDS * scene_fps))
        track = find_track(context)
        clips = () if track is None else track.clips
        if not tracks.fill_ranges(
            clips, self.frame, max_length, scene.frame_start, scene.frame_end,
        ):
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        try:
            track = track or ensure_track(context)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not edit_track(
            track, tracks.fill_gap, self.frame, max_length,
            scene.frame_start, scene.frame_end, prompt=prompt,
        ):
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        scene.clip_data.new_clip_prompt = ""
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_shrink_neighbors(Operator):
    """Make more room by trimming a neighboring clip to the current frame. Place the current frame inside that clip first"""
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
    """Adjust the settings and generate a take. Leave Override empty to use the clip's prompt"""
    bl_idname = "clips.edit_prompt"
    bl_label = "Generate Take"
    bl_options = {'UNDO'}
    prompt: StringProperty(
        name="Override", options={'SKIP_SAVE'},
        description="Optional prompt for this take. Leave empty to use the clip's prompt",
    )
    seed: IntProperty(
        name="Seed", min=-1, default=-1, description="-1 chooses a new random seed",
        options={'SKIP_SAVE'},
    )
    model: EnumProperty(
        name="Model", description="Generation model", items=models.get_model_enum_items,
        options={'SKIP_SAVE'},
    )
    temperature: FloatProperty(
        name="Temperature", min=0.001, default=Config.DEFAULT_TEMPERATURE,
        description="Sampling temperature for text generation", options={'SKIP_SAVE'},
    )
    cycled: BoolProperty(
        name="Cycled", description="Request a looping motion", options={'SKIP_SAVE'},
    )
    in_place: BoolProperty(
        name="In Place", description="Request motion in place", options={'SKIP_SAVE'},
    )
    enhance_prompt: BoolProperty(
        name="Enhance Prompt", default=True,
        description="Ask the service to enhance the submitted prompt", options={'SKIP_SAVE'},
    )

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
        self.enhance_prompt = clip.generation_enhance_prompt
        return context.window_manager.invoke_props_dialog(self, width=STYLE.prompt_dialog_width)

    def draw(self, context):
        col = self.layout.column()
        _, clip = self._get_clip(context)
        row = col.row()
        row.activate_init = True
        row.prop(self, "prompt", placeholder=clip.name if clip is not None else "")
        col.prop(self, "seed")
        row = col.row(align=True)
        row.prop(self, "model")
        row.prop(self, "temperature")
        row = col.row(align=True)
        row.prop(self, "cycled")
        row.prop(self, "in_place")
        row.prop(self, "enhance_prompt")

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.prompt = self.prompt.strip()
        clip.seed = self.seed
        clip.generation_model = self.model
        clip.generation_temperature = self.temperature
        clip.generation_cycled = self.cycled
        clip.generation_in_place = self.in_place
        clip.generation_enhance_prompt = self.enhance_prompt
        if not self._generate_take(context, track, clip):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_new_take(_ClipOp, Operator):
    """Create an animation for this clip. Watch its progress and compare results in Takes"""
    bl_idname = "clips.new_take"
    bl_label = "New Take"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return runtime.generation_ready()

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        if not self._generate_take(context, track, clip):
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_cancel_take(_ClipOp, Operator):
    """Stop this generation. Click Generate Take when you want to try again"""
    bl_idname = "clips.cancel_take"
    bl_label = "Cancel Take"
    take_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        track, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        operations.cancel_take_generation(track, clip, self.take_uid)
        tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_takes(_ClipOp, Operator):
    """Remove all takes and animation in this clip, including your edits. Keep the clip and prompt to start again"""
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
    """Remove this take. If it is selected and no finished take remains, the clip's animation is cleared, including your edits"""
    bl_idname = "clips.delete_take"
    bl_label = "Delete Take"
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
    """Remove this clip and its animation, including your edits. Use Bake Clip to keep the animation"""
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
    """Keep the animation you see and remove the clip and its takes. Continue editing the animation in Blender"""
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
    """Copy this clip and its finished takes. Right-click empty space and choose Paste Clip"""
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
    """Place a copy of the clip here. There must be enough empty space to fit it"""
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
    """Use this take and its settings. Select other takes to compare animations"""
    bl_idname = "clips.set_take"
    bl_label = "Choose Take"
    bl_options = {'UNDO'}
    take_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        track = find_track(context)
        clip = tracks.find_clip(
            track.clips, properties.clip_uid or track.active_clip_uid,
        ) if track is not None else None
        take = takes.find_take(clip, properties.take_uid) if clip is not None else None
        if take is None:
            return cls.__doc__
        return cls.__doc__ + "\n\nGeneration history:\n" + take_description(take)

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
    """Join this clip to its neighbors and automatically generate a take for its new length"""
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
    """Use the current frame as the start or end. Start moves the clip; End changes its length"""
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
