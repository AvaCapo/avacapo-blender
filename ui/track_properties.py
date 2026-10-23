"""Blender storage for timeline records and the boundary for core mutations."""

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, FloatProperty, IntProperty, PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from ..common.config import Config, TimelineConfig
from ..core import tracks


_writing_properties = False


class BlenderTrackStore:
    """Allocation only; UID assignment and timeline rules belong to core."""

    def __init__(self, data):
        self.data = data

    @property
    def clips(self):
        return self.data.clips

    def allocate_clip(self):
        return self.clips.add()

    def remove_clip_record(self, uid):
        for index, clip in enumerate(self.clips):
            if clip.uid == uid:
                self.clips.remove(index)
                return

    def allocate_take(self, clip):
        return clip.takes.add()

    def clear_take_records(self, clip):
        clip.takes.clear()


def edit_track(data, operation, *args, **kwargs):
    """Run a core mutation without re-entering RNA callbacks, including nested calls."""
    global _writing_properties
    previous = _writing_properties
    _writing_properties = True
    try:
        return operation(BlenderTrackStore(data), *args, **kwargs)
    finally:
        _writing_properties = previous


class CT_Take(PropertyGroup):
    uid: IntProperty(options={'HIDDEN'})
    name: StringProperty(name="Name", default="Take")
    prompt: StringProperty(name="Prompt")
    generation_mode: StringProperty(default="text", options={'HIDDEN'})
    generation_name: StringProperty(options={'HIDDEN'})
    generation_model: StringProperty(default=Config.DEFAULT_MODEL_TYPE, options={'HIDDEN'})
    generation_in_place: BoolProperty(options={'HIDDEN'})
    duration_seconds: FloatProperty(default=Config.DEFAULT_DURATION, options={'HIDDEN'})
    fps: FloatProperty(default=Config.DEFAULT_FPS, options={'HIDDEN'})
    temperature: FloatProperty(default=Config.DEFAULT_TEMPERATURE, options={'HIDDEN'})
    cycled: BoolProperty(options={'HIDDEN'})
    seed: IntProperty(name="Seed", min=0, description="0 = random")
    left_frame: IntProperty(min=0, options={'HIDDEN'})
    right_frame: IntProperty(min=0, options={'HIDDEN'})
    left_context_frames: IntProperty(default=1, min=1, options={'HIDDEN'})
    right_context_frames: IntProperty(default=1, min=1, options={'HIDDEN'})
    gap_frames: IntProperty(default=150, min=1, options={'HIDDEN'})
    end_offset_x: FloatProperty(options={'HIDDEN'})
    end_offset_z: FloatProperty(options={'HIDDEN'})
    align_heading: BoolProperty(default=True, options={'HIDDEN'})
    take_action: PointerProperty(type=bpy.types.Action, options={'HIDDEN'})
    take_slot_handle: IntProperty(options={'HIDDEN'})
    offset: IntProperty(
        name="Offset", description="Frames from the clip start to the take start. "
        "Moving the clip does not change it")
    length: IntProperty(
        name="Length", min=TimelineConfig.MIN_TAKE_LENGTH,
        default=TimelineConfig.MIN_TAKE_LENGTH, options={'HIDDEN'})
    progress: FloatProperty(
        name="Progress", min=0.0, max=1.0, default=1.0, subtype='FACTOR')


class CT_Clip(PropertyGroup):
    uid: IntProperty()
    name: StringProperty(name="Name", default="Clip")

    def _get_start(self):
        return self.get("_start", 0)

    def _set_start(self, value):
        if _writing_properties:
            self["_start"] = int(value)
        else:
            edit_track(self.id_data.clip_data, tracks.resize_start, self.uid, value)

    def _get_end(self):
        return self.get("_end", 1)

    def _set_end(self, value):
        if _writing_properties:
            self["_end"] = int(value)
        else:
            edit_track(self.id_data.clip_data, tracks.resize_end, self.uid, value)

    def _get_duration(self):
        return self.end - self.start

    def _set_duration(self, value):
        edit_track(self.id_data.clip_data, tracks.set_duration, self.uid, value)

    def _on_bind_changed(self, context):
        if not _writing_properties:
            edit_track(self.id_data.clip_data, tracks.apply_bindings)

    start: IntProperty(name="Start", get=_get_start, set=_set_start)
    end: IntProperty(name="End", get=_get_end, set=_set_end)
    duration: IntProperty(
        name="Duration", min=TimelineConfig.MIN_CLIP_LENGTH,
        get=_get_duration, set=_set_duration)
    bind_start: BoolProperty(
        name="Stick to Previous Clip", update=_on_bind_changed,
        description="Keep the start glued to the end of the previous clip")
    bind_end: BoolProperty(
        name="Stick to Next Clip", update=_on_bind_changed,
        description="Keep the end glued to the start of the next clip")
    def _get_active_take_index(self):
        return next(
            (index for index, take in enumerate(self.takes)
             if take.uid == self.active_take_uid),
            0,
        )

    def _set_active_take_index(self, value):
        if _writing_properties or not 0 <= value < len(self.takes):
            return
        from ..animation import test_take_generation
        test_take_generation.switch_take(
            self.id_data,
            self,
            self.takes[value].uid,
        )

    takes: CollectionProperty(type=CT_Take)
    active_take_uid: IntProperty(options={'HIDDEN'})
    # Blender's template_list requires an index. It is derived from the UID
    # each time and is never saved as another active-Take state.
    active_take_index: IntProperty(
        get=_get_active_take_index, set=_set_active_take_index, min=0,
    )
    take_counter: IntProperty(default=0, options={'HIDDEN'})
    target_object: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    target_action: PointerProperty(type=bpy.types.Action, options={'HIDDEN'})
    target_slot_handle: IntProperty(options={'HIDDEN'})
    binding_initialized: BoolProperty(options={'HIDDEN'})


class CT_Scene(PropertyGroup):
    clips: CollectionProperty(type=CT_Clip)
    active_uid: IntProperty(default=0)
    next_uid: IntProperty(default=1)
    working_object: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    working_action: PointerProperty(type=bpy.types.Action, options={'HIDDEN'})
    working_slot_handle: IntProperty(options={'HIDDEN'})
    default_length: IntProperty(
        name="Default Length", min=TimelineConfig.MIN_CLIP_LENGTH,
        default=TimelineConfig.DEFAULT_CLIP_LENGTH,
        description="Length of a new clip when there is room for it")
