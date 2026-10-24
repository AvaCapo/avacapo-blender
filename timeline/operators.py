"""Timeline commands, generation dialogs, and user-facing reports."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

from ..animation import test_take_generation
from ..common.config import Config
from ..core import takes, tracks
from ..core.generation_models import GenerationCommand, TextParameters
from .properties import edit_track
from .runtime import runtime, tag_redraw
from .track_style import STYLE


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
        options={'HIDDEN', 'SKIP_SAVE'},
    )

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
        items=[('START', "Start", ""), ('END', "End", "")], options={'HIDDEN', 'SKIP_SAVE'},
    )

    def execute(self, context):
        data, clip = self._get_clip(context)
        if clip is None:
            return {'CANCELLED'}
        operation = tracks.resize_start if self.which == 'START' else tracks.resize_end
        edit_track(data, operation, clip.uid, context.scene.frame_current)
        tag_redraw()
        return {'FINISHED'}
