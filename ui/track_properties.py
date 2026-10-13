"""Blender storage for timeline records and the boundary for core mutations."""

from bpy.props import (
    BoolProperty, CollectionProperty, FloatProperty, IntProperty, StringProperty,
)
from bpy.types import PropertyGroup

from ..common.config import TimelineConfig
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
    uid: IntProperty()
    name: StringProperty(name="Name", default="Take")
    prompt: StringProperty(name="Prompt")
    seed: IntProperty(name="Seed", min=0)
    offset: IntProperty(
        name="Offset", description="Frames from the clip start to the take start. "
        "Moving the clip does not change it")
    length: IntProperty(
        name="Length", min=TimelineConfig.MIN_TAKE_LENGTH,
        default=TimelineConfig.MIN_TAKE_LENGTH)
    progress: FloatProperty(
        name="Progress", min=0.0, max=1.0, default=1.0, subtype='FACTOR')


class CT_Clip(PropertyGroup):
    uid: IntProperty()
    name: StringProperty(name="Name", default="Clip")
    prompt: StringProperty(name="Prompt")
    seed: IntProperty(name="Seed", min=0, description="0 = random")

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
    takes: CollectionProperty(type=CT_Take)
    active_take: IntProperty(default=0, min=0)
    take_counter: IntProperty(default=0)


class CT_Scene(PropertyGroup):
    clips: CollectionProperty(type=CT_Clip)
    active_uid: IntProperty(default=0)
    next_uid: IntProperty(default=1)
    default_length: IntProperty(
        name="Default Length", min=TimelineConfig.MIN_CLIP_LENGTH,
        default=TimelineConfig.DEFAULT_CLIP_LENGTH,
        description="Length of a new clip when there is room for it")
