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


def find_track(context):
    """Find the Track for the current Object, Action, and Slot without creating data."""
    scene = context.scene
    armature = context.active_object
    if armature is None or armature.type != 'ARMATURE' or not armature.select_get():
        return None
    animation_data = armature.animation_data
    if animation_data is None or animation_data.action is None or animation_data.action_slot is None:
        return None
    action = animation_data.action
    slot = animation_data.action_slot
    return next(
        (
            track for track in scene.clip_data.tracks
            if track.object == armature
            and track.working_action == action
            and int(track.working_slot_handle) == int(slot.handle)
        ),
        None,
    )


def ensure_track(context):
    """Find or create the Track for an explicit Timeline mutation."""
    scene = context.scene
    armature = context.active_object
    if armature is None or armature.type != 'ARMATURE' or not armature.select_get():
        raise ValueError("Select an armature before changing the Timeline.")

    animation_data = armature.animation_data
    data = scene.clip_data
    previous_active_track_uid = data.active_track_uid
    previous_next_track_uid = data.next_track_uid
    created_action = None
    created_track = None
    created_animation_data = animation_data is None
    try:
        if animation_data is None:
            animation_data = armature.animation_data_create()
        action = animation_data.action
        slot = animation_data.action_slot
        if action is None:
            action = bpy.data.actions.new(name="AvaCapo Working Action")
            created_action = action
            slot = action.slots.new(armature.id_type, name=armature.name)
            animation_data.action = action
            animation_data.action_slot = slot
        elif slot is None:
            raise ValueError("Current Working Action has no selected Slot.")

        slots = [candidate for candidate in action.slots if candidate.handle == slot.handle]
        if len(slots) != 1:
            raise ValueError("Current Working Action Slot is missing or ambiguous.")

        for existing_track in data.tracks:
            for clip in existing_track.clips:
                for take in clip.takes:
                    if take.take_action == action:
                        raise ValueError("A Take Action cannot be used as a Working Action.")

        track = next(
            (
                candidate for candidate in data.tracks
                if candidate.object == armature
                and candidate.working_action == action
                and int(candidate.working_slot_handle) == int(slot.handle)
            ),
            None,
        )
        if track is None:
            track = data.tracks.add()
            created_track = track
            track.uid = data.next_track_uid
            data.next_track_uid += 1
            track.name = f"Track {track.uid}"
            track.object = armature
            track.working_action = action
            track.working_slot_handle = int(slot.handle)
        data.active_track_uid = track.uid
        return track
    except Exception:
        if created_track is not None:
            for index, candidate in enumerate(data.tracks):
                if candidate == created_track:
                    data.tracks.remove(index)
                    break
            data.next_track_uid = previous_next_track_uid
        data.active_track_uid = previous_active_track_uid
        if created_action is not None:
            animation_data.action = None
            animation_data.action_slot = None
            bpy.data.actions.remove(created_action)
        if created_animation_data:
            armature.animation_data_clear()
        raise


def track_for_clip(scene, clip):
    """Return the Track that owns this RNA Clip record."""
    for track in scene.clip_data.tracks:
        if any(candidate == clip for candidate in track.clips):
            return track
    return None


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
            track = track_for_clip(self.id_data, self)
            if track is not None:
                edit_track(track, tracks.resize_start, self.uid, value)

    def _get_end(self):
        return self.get("_end", 1)

    def _set_end(self, value):
        if _writing_properties:
            self["_end"] = int(value)
        else:
            track = track_for_clip(self.id_data, self)
            if track is not None:
                edit_track(track, tracks.resize_end, self.uid, value)

    def _get_duration(self):
        return self.end - self.start

    def _set_duration(self, value):
        track = track_for_clip(self.id_data, self)
        if track is not None:
            edit_track(track, tracks.set_duration, self.uid, value)

    def _on_bind_changed(self, context):
        if not _writing_properties:
            track = track_for_clip(self.id_data, self)
            if track is not None:
                edit_track(track, tracks.apply_bindings)

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
        # Selection is an explicit `clips.set_take` command. template_list
        # still needs a setter for its derived active index.
        return None

    takes: CollectionProperty(type=CT_Take)
    active_take_uid: IntProperty(options={'HIDDEN'})
    # Blender's template_list requires an index. It is derived from the UID
    # each time and is never saved as another active-Take state.
    active_take_index: IntProperty(
        get=_get_active_take_index, set=_set_active_take_index, min=0,
    )
    take_counter: IntProperty(default=0, options={'HIDDEN'})


class CT_Track(PropertyGroup):
    uid: IntProperty(options={'HIDDEN'})
    name: StringProperty(name="Track")
    object: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    working_action: PointerProperty(type=bpy.types.Action, options={'HIDDEN'})
    working_slot_handle: IntProperty(options={'HIDDEN'})
    active_clip_uid: IntProperty(default=0, options={'HIDDEN'})
    next_clip_uid: IntProperty(default=1, options={'HIDDEN'})
    clips: CollectionProperty(type=CT_Clip)


class CT_Scene(PropertyGroup):
    active_track_uid: IntProperty(default=0, options={'HIDDEN'})
    next_track_uid: IntProperty(default=1, options={'HIDDEN'})
    tracks: CollectionProperty(type=CT_Track)
    default_length: IntProperty(
        name="Default Length", min=TimelineConfig.MIN_CLIP_LENGTH,
        default=TimelineConfig.DEFAULT_CLIP_LENGTH,
        description="Length of a new clip when there is room for it")
