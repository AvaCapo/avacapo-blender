import bpy
import math
import os
import threading
import time
import webbrowser
import uuid
import textwrap

from bpy.app.handlers import persistent

from .state_controller import State, frame_change_post
from .server import (
    get_animation,
    get_animation_constraints,
    get_animation_inbetween,
    get_fps,
)
from . import animation_utils
from . import bvh_smpl
from . import constraint_utils
from . import rig_utils
from .storage import Storage
from .logger import log
from .config import Config
from .models import clear_models_cache, get_model_enum_items, resolve_model_type
from .ui import AVACAPO_PT_main_panel
from .update_checker import reset_update_check_state, start_update_check
from .version import ADDON_VERSION

config = Config()
bl_info = {
    "name": "AvaCapo AI animation",
    "author": "agamurian",
    "version": ADDON_VERSION,
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Avacapo",
    "description": "Automating charecter animation with AI",
    "category": "Animation",
}

_SOURCE_FRAME_VALUE_KEY = "_constraint_source_frame_value"
_TARGET_FRAME_VALUE_KEY = "_constraint_target_frame_value"


def _constraint_source_frame_get(settings) -> int:
    if settings.constraint_pose_source == "CURRENT_POSE":
        return 0
    if _SOURCE_FRAME_VALUE_KEY in settings:
        return max(0, int(settings[_SOURCE_FRAME_VALUE_KEY]))

    scene = getattr(settings, "id_data", None)
    if scene is None:
        scene = bpy.context.scene
    return max(
        0,
        constraint_utils.source_frame_count(scene, settings.constraint_pose_source) - 1,
    )


def _constraint_source_frame_set(settings, value: int) -> None:
    if settings.constraint_pose_source == "CURRENT_POSE":
        settings[_SOURCE_FRAME_VALUE_KEY] = 0
    else:
        settings[_SOURCE_FRAME_VALUE_KEY] = max(0, int(value))


def _constraint_pose_source_update(settings, _context) -> None:
    if _SOURCE_FRAME_VALUE_KEY in settings:
        del settings[_SOURCE_FRAME_VALUE_KEY]


def _constraint_target_frame_get(settings) -> int:
    return max(0, int(settings.get(_TARGET_FRAME_VALUE_KEY, 0)))


def _constraint_target_frame_set(settings, value: int) -> None:
    requested = max(0, int(value))
    max_frame = constraint_utils.output_frame_count(settings) - 1
    if requested > max_frame:
        settings.constraint_target_error = (
            f"Target Frame {requested} is outside 0..{max_frame}; " f"using {max_frame}"
        )
        requested = max_frame
    else:
        settings.constraint_target_error = ""
    settings[_TARGET_FRAME_VALUE_KEY] = requested


def _revalidate_constraint_target(settings) -> None:
    current = int(settings.get(_TARGET_FRAME_VALUE_KEY, 0))
    _constraint_target_frame_set(settings, current)


class AvacapoConstraint(bpy.types.PropertyGroup):
    uid: bpy.props.StringProperty()
    constraint_input: bpy.props.StringProperty(default="POSE")
    constraint_type: bpy.props.StringProperty(default="fullbody")
    constraint_joint_name: bpy.props.EnumProperty(
        items=constraint_utils.END_EFFECTOR_ITEMS,
        options={"ENUM_FLAG"},
        default={"LeftFoot"},
    )
    source_armature: bpy.props.PointerProperty(
        type=bpy.types.Object,
        poll=constraint_utils.armature_poll,
    )
    target_object: bpy.props.PointerProperty(
        type=bpy.types.Object,
        poll=constraint_utils.object_target_poll,
    )
    target_offset: bpy.props.FloatVectorProperty(
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype="TRANSLATION",
    )
    pose_source: bpy.props.StringProperty(default="CURRENT_POSE")
    source_frame: bpy.props.IntProperty(default=0, min=0)
    source_frame_count: bpy.props.IntProperty(default=1, min=0)
    target_frame: bpy.props.IntProperty(default=0, min=0)
    direction: bpy.props.FloatVectorProperty(
        default=(0.0, 1.0, 0.0),
        size=3,
    )


class AvacapoSettings(bpy.types.PropertyGroup):
    _updating_time = False

    def _fps(self) -> int:
        return max(1, int(get_fps()))

    def _set_time_fields(self, callback) -> None:
        cls = type(self)
        if cls._updating_time:
            return
        cls._updating_time = True
        try:
            callback()
        finally:
            cls._updating_time = False

    def update_start(self, context):
        self._set_time_fields(
            lambda: setattr(self, "end", int(round(self.start + (self.duration * self._fps()))))
        )
        _revalidate_constraint_target(self)

    def update_duration(self, context):
        self._set_time_fields(
            lambda: setattr(self, "end", int(round(self.start + (self.duration * self._fps()))))
        )
        _revalidate_constraint_target(self)

    def update_end(self, context):
        self._set_time_fields(
            lambda: setattr(self, "duration", (self.end - self.start) / self._fps())
        )
        _revalidate_constraint_target(self)

    text_block: bpy.props.PointerProperty(type=bpy.types.Text)
    animation_mode: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=[("NLA", "NLA", "Use nla editor for animation")],
    )
    prompt: bpy.props.StringProperty(
        name="", default="walking backward", description="Enter text here"
    )
    start_record_lock: bpy.props.BoolProperty(
        name="start_record_lock",
        default=False,
    )
    start: bpy.props.IntProperty(
        name="start",
        default=0,
        update=update_start,
    )
    duration: bpy.props.FloatProperty(
        name="duration",
        description="seconds",
        default=5,
        update=update_duration,
    )
    end: bpy.props.IntProperty(
        name="end",
        default=int(config.DEFAULT_DURATION * get_fps()),
        update=update_end,
    )
    transition: bpy.props.IntProperty(
        name="transition",
        description="frames of overlap when chaining clips",
        default=10,
        min=0,
    )
    fadein: bpy.props.IntProperty(
        name="fadein",
        description="frames",
        default=0,
    )
    fadeout: bpy.props.IntProperty(
        name="fadeout",
        description="frames",
        default=0,
    )
    temperature: bpy.props.FloatProperty(
        name="temperature",
        description="setting 0.0 - 1.0 for generation temperature",
        default=config.DEFAULT_TEMPERATURE,
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=get_model_enum_items,
    )
    in_place: bpy.props.BoolProperty(
        name="In Place",
        description="Generate animation without root motion translation",
        default=False,
    )
    generation_mode: bpy.props.EnumProperty(
        name="Generation",
        description="Choose motion generation or inbetweening",
        items=(
            ("STANDARD", "Generation", "Generate motion from a text prompt", 0),
            (
                "INBETWEEN",
                "Inbetweening",
                "Generate a transition between two armature animations",
                2,
            ),
        ),
        default="STANDARD",
    )
    inbetween_left_armature: bpy.props.PointerProperty(
        name="First Armature",
        description="Armature whose animation is placed before the generated transition",
        type=bpy.types.Object,
        poll=constraint_utils.armature_poll,
    )
    inbetween_right_armature: bpy.props.PointerProperty(
        name="Second Armature",
        description="Armature whose animation is placed after the generated transition",
        type=bpy.types.Object,
        poll=constraint_utils.armature_poll,
    )
    inbetween_source_mode: bpy.props.EnumProperty(
        name="Inbetween Source",
        description="Choose two armatures or two selected keyframes in the active action",
        items=(
            (
                "ARMATURES",
                "Two Armatures",
                "Generate between animations on two different armatures",
                0,
            ),
            (
                "TIMELINE",
                "Selected Keyframes",
                "Insert generated motion between two selected keyframes of the active armature",
                1,
            ),
        ),
        default="ARMATURES",
    )
    show_constraint_settings: bpy.props.BoolProperty(
        name="Constraints",
        description="Show or hide saved constraints",
        default=True,
    )
    show_constraint_editor: bpy.props.BoolProperty(
        default=False, options={"HIDDEN", "SKIP_SAVE"}
    )
    constraint_edit_index: bpy.props.IntProperty(
        default=-1, options={"HIDDEN", "SKIP_SAVE"}
    )
    constraints: bpy.props.CollectionProperty(type=AvacapoConstraint)
    constraint_input: bpy.props.EnumProperty(
        name="Input",
        description="Type of data used to constrain generation",
        items=(
            ("POSE", "Pose", "Use a pose or animation range from an armature"),
            (
                "OBJECT_TARGET",
                "Object Target",
                "Reach an object or Empty with one hand or foot",
            ),
            ("DIRECTION", "Direction", "Use a direction vector"),
        ),
        default="POSE",
    )
    constraint_type: bpy.props.EnumProperty(
        name="Type",
        description="Body region affected by the constraint",
        items=constraint_utils.CONSTRAINT_TYPE_ITEMS,
        default="fullbody",
    )
    constraint_joint_name: bpy.props.EnumProperty(
        name="Joints",
        description="End effectors constrained by the input",
        items=constraint_utils.END_EFFECTOR_ITEMS,
        options={"ENUM_FLAG"},
        default={"LeftFoot"},
    )
    constraint_object_target_joint: bpy.props.EnumProperty(
        name="End Effector",
        description="Single hand or foot that should reach the target",
        items=constraint_utils.OBJECT_TARGET_ITEMS,
        default="LeftHand",
    )
    constraint_source_armature: bpy.props.PointerProperty(
        name="Armature",
        description=(
            "Armature whose evaluated pose is converted to NPZ; " "empty uses the active armature"
        ),
        type=bpy.types.Object,
        poll=constraint_utils.armature_poll,
    )
    constraint_target_object: bpy.props.PointerProperty(
        name="Target",
        description="Object origin or Empty used as the end-effector target",
        type=bpy.types.Object,
        poll=constraint_utils.object_target_poll,
    )
    constraint_target_offset: bpy.props.FloatVectorProperty(
        name="Target Offset",
        description="Target-local offset from the selected object's origin",
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype="TRANSLATION",
    )
    constraint_pose_source: bpy.props.EnumProperty(
        name="Pose Source",
        description="Sample one pose or the Timeline Preview Range",
        items=(
            ("CURRENT_POSE", "Current Pose", "Sample the armature at the current frame"),
            ("PREVIEW_RANGE", "Preview Range", "Sample every frame in the Timeline Preview Range"),
        ),
        default="CURRENT_POSE",
        update=_constraint_pose_source_update,
    )
    constraint_source_frame: bpy.props.IntProperty(
        name="Source Frame",
        description="Zero-based frame inside the pose NPZ used as the constraint",
        min=0,
        get=_constraint_source_frame_get,
        set=_constraint_source_frame_set,
    )
    constraint_target_frame: bpy.props.IntProperty(
        name="Target Frame",
        description="Zero-based generated frame at which the pose must be reached",
        min=0,
        get=_constraint_target_frame_get,
        set=_constraint_target_frame_set,
    )
    constraint_target_error: bpy.props.StringProperty(default="", options={"HIDDEN"})
    constraint_text_weight: bpy.props.FloatProperty(
        name="Text Weight",
        description="Influence of the text prompt",
        default=2.0,
        min=0.0,
    )
    constraint_weight: bpy.props.FloatProperty(
        name="Constraint Weight",
        description="Influence of the pose or direction constraint",
        default=2.5,
        min=0.0,
    )
    constraint_first_heading: bpy.props.FloatProperty(
        name="First Heading",
        description="Heading used for the first generated frame",
        default=0.0,
    )
    constraint_direction: bpy.props.FloatVectorProperty(
        name="Direction",
        description="Desired motion direction",
        default=(0.0, 1.0, 0.0),
        size=3,
        subtype="DIRECTION",
    )
    token_input: bpy.props.StringProperty(
        name="Token",
        description="Paste your API token here",
        default="",
        subtype="PASSWORD",
    )


# stored locally (on Object), bound to single nla track
# and have several "attempts"(requests/actions)
# attempt - a single trial
class AvacapoAttempt(bpy.types.PropertyGroup):

    uid: bpy.props.StringProperty()
    name: bpy.props.StringProperty()
    action_name: bpy.props.StringProperty()
    status: bpy.props.StringProperty()  # | Error | Pending | Fetching | Done

    temperature: bpy.props.FloatProperty(
        name="temperature",
        description="setting 0.0 - 1.0 for generation temperature",
        default=config.DEFAULT_TEMPERATURE,
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=get_model_enum_items,
    )
    in_place: bpy.props.BoolProperty(
        name="In Place",
        description="Generate animation without root motion translation",
        default=False,
    )
    prompt: bpy.props.StringProperty()
    duration: bpy.props.FloatProperty()
    constraints: bpy.props.CollectionProperty(type=AvacapoConstraint)
    use_constraints: bpy.props.BoolProperty(default=False)
    constraint_input: bpy.props.StringProperty(default="POSE")
    constraint_type: bpy.props.StringProperty(default="fullbody")
    constraint_joint_name: bpy.props.EnumProperty(
        items=constraint_utils.END_EFFECTOR_ITEMS,
        options={"ENUM_FLAG"},
        default={"LeftFoot"},
    )
    constraint_source_frame: bpy.props.IntProperty(default=0, min=0)
    constraint_target_frame: bpy.props.IntProperty(default=0, min=0)
    constraint_num_frames: bpy.props.IntProperty(default=1, min=1)
    constraint_text_weight: bpy.props.FloatProperty(default=2.0, min=0.0)
    constraint_weight: bpy.props.FloatProperty(default=2.5, min=0.0)
    constraint_first_heading: bpy.props.FloatProperty(default=0.0)
    constraint_direction: bpy.props.FloatVectorProperty(
        default=(0.0, 1.0, 0.0),
        size=3,
    )


class AvacapoClip(bpy.types.PropertyGroup):
    # nla_track.name = clip_name
    def create_name(self) -> str:
        max_len = 16
        uid = str(uuid.uuid4().hex[:6])
        if len(self.prompt) > max_len:
            words = self.prompt[:max_len].split(" ")[:-1]
        else:
            words = self.prompt.split(" ")
        return "_".join([*words, uid])

    prompt: bpy.props.StringProperty(
        name="", default="walking backward", description="Enter text here"
    )
    name: bpy.props.StringProperty()
    temperature: bpy.props.FloatProperty(
        name="temperature",
        description="setting 0.0 - 1.0 for neural network 'randomness'",
        default=config.DEFAULT_TEMPERATURE,
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=get_model_enum_items,
    )
    in_place: bpy.props.BoolProperty(
        name="In Place",
        description="Generate animation without root motion translation",
        default=False,
    )

    attempts: bpy.props.CollectionProperty(type=AvacapoAttempt)
    active_attempt: bpy.props.StringProperty()


class AvacapoClips(bpy.types.PropertyGroup):
    clips: bpy.props.CollectionProperty(type=AvacapoClip)


def _frame_count_from_bounds(start_frame: float, end_frame: float) -> int:
    return max(1, int(round(end_frame - start_frame)))


def _frame_count_from_action(action: bpy.types.Action) -> int:
    frame_start, frame_end = action.frame_range
    return max(1, int(round(frame_end - frame_start + 1)))


def _frame_count_from_strip(nla_strip: bpy.types.NlaStrip) -> int:
    return _frame_count_from_bounds(nla_strip.frame_start_ui, nla_strip.frame_end_ui)


def _configure_strip_timing(
    nla_strip: bpy.types.NlaStrip,
    *,
    start_frame: float,
    frame_count: int,
    blend_in: int,
    blend_out: int,
) -> None:
    safe_frame_count = max(1, int(frame_count))
    max_blend = max(0, safe_frame_count - 1)

    nla_strip.frame_start_ui = int(round(start_frame))
    nla_strip.frame_end_ui = int(round(start_frame + safe_frame_count))
    nla_strip.action_frame_start = 1
    nla_strip.action_frame_end = safe_frame_count
    nla_strip.use_auto_blend = False
    nla_strip.blend_in = min(int(blend_in), max_blend)
    nla_strip.blend_out = min(int(blend_out), max_blend)
    nla_strip.blend_type = "REPLACE"


def _sync_strip_to_action(nla_strip: bpy.types.NlaStrip, action: bpy.types.Action) -> None:
    _configure_strip_timing(
        nla_strip,
        start_frame=nla_strip.frame_start_ui,
        frame_count=_frame_count_from_action(action),
        blend_in=int(nla_strip.blend_in),
        blend_out=int(nla_strip.blend_out),
    )


def _find_latest_strip(obj: bpy.types.Object) -> bpy.types.NlaStrip | None:
    if obj.animation_data is None:
        return None

    latest_strip = None
    latest_end = float("-inf")
    for nla_track in obj.animation_data.nla_tracks:
        for nla_strip in nla_track.strips:
            if nla_strip.frame_end_ui > latest_end:
                latest_end = nla_strip.frame_end_ui
                latest_strip = nla_strip
    return latest_strip


def _iter_all_strips(obj: bpy.types.Object):
    if obj.animation_data is None:
        return

    for nla_track in obj.animation_data.nla_tracks:
        for nla_strip in nla_track.strips:
            yield nla_strip


def _find_clip_by_name(obj: bpy.types.Object, clip_name: str):
    return next((clip for clip in obj.avacapo_clips.clips if clip.name == clip_name), None)


def _get_strip_for_clip(obj: bpy.types.Object, clip) -> bpy.types.NlaStrip | None:
    if obj.animation_data is None:
        return None

    nla_track = obj.animation_data.nla_tracks.get(clip.name)
    if nla_track is None:
        return None

    return nla_track.strips.get(clip.name)


def _find_clip_at_exact_range(obj: bpy.types.Object, start_frame: float, end_frame: float):
    target_start = int(round(start_frame))
    target_frame_count = _frame_count_from_bounds(start_frame, end_frame)

    for clip in obj.avacapo_clips.clips:
        nla_strip = _get_strip_for_clip(obj, clip)
        if nla_strip is None:
            continue

        strip_start = int(round(nla_strip.frame_start_ui))
        strip_frame_count = _frame_count_from_strip(nla_strip)

        # BVH import can shift the final strip length by one frame after apply.
        # Treat same-start clips with near-identical duration as the same range.
        if strip_start == target_start and abs(strip_frame_count - target_frame_count) <= 1:
            return clip, nla_strip

    return None, None


def _create_attempt_for_clip(
    obj: bpy.types.Object,
    clip,
    *,
    prompt: str,
    model: str,
    in_place: bool,
    duration: float,
):
    new_attempt = clip.attempts.add()
    new_attempt.uid = uuid.uuid4().hex[:6]
    new_attempt.prompt = prompt
    new_attempt.model = model
    new_attempt.in_place = in_place

    n = len(clip.attempts)
    new_attempt.name = f"Take_{n}_{model}"
    new_attempt.action_name = f"{clip.name}_{n}"
    new_attempt.duration = duration

    action = bpy.data.actions.new(name=new_attempt.action_name)
    slot = action.slots.new(obj.id_type, name=obj.name)
    clip.active_attempt = new_attempt.uid
    return new_attempt, action, slot


def _constraint_record(constraint) -> dict:
    return {
        "payload_key": str(constraint.uid),
        "constraint_input": str(constraint.constraint_input),
        "constraint_type": str(constraint.constraint_type),
        "joint_names": constraint_utils.selected_joint_names(constraint.constraint_joint_name),
        "source_armature": constraint.source_armature,
        "target_object": constraint.target_object,
        "target_offset": tuple(float(value) for value in constraint.target_offset),
        "pose_source": str(constraint.pose_source),
        "source_frame": int(constraint.source_frame),
        "source_frame_count": int(constraint.source_frame_count),
        "target_frame": int(constraint.target_frame),
        "direction": tuple(float(value) for value in constraint.direction),
    }


def _attempt_constraint_records(attempt) -> list[dict]:
    records = [_constraint_record(constraint) for constraint in attempt.constraints]
    if records or not attempt.use_constraints:
        return records

    # Compatibility with constrained takes saved before multiple constraints.
    return [
        {
            "payload_key": str(attempt.uid),
            "constraint_input": str(attempt.constraint_input),
            "constraint_type": str(attempt.constraint_type),
            "joint_names": constraint_utils.selected_joint_names(attempt.constraint_joint_name),
            "source_armature": None,
            "target_object": None,
            "target_offset": (0.0, 0.0, 0.0),
            "pose_source": "CURRENT_POSE",
            "source_frame": int(attempt.constraint_source_frame),
            "source_frame_count": max(1, int(attempt.constraint_source_frame) + 1),
            "target_frame": int(attempt.constraint_target_frame),
            "direction": tuple(float(value) for value in attempt.constraint_direction),
        }
    ]


def _configure_attempt_constraints(
    attempt,
    records,
    *,
    num_frames: int,
    text_weight: float,
    constraint_weight: float,
    first_heading: float,
) -> None:
    attempt.constraints.clear()
    attempt.use_constraints = bool(records)
    attempt.constraint_num_frames = max(1, int(num_frames))
    attempt.constraint_text_weight = float(text_weight)
    attempt.constraint_weight = float(constraint_weight)
    attempt.constraint_first_heading = float(first_heading)

    for record in records:
        stored = attempt.constraints.add()
        stored.uid = uuid.uuid4().hex
        stored.constraint_input = record["constraint_input"]
        stored.constraint_type = record["constraint_type"]
        stored.constraint_joint_name = set(record["joint_names"])
        stored.source_armature = record.get("source_armature")
        stored.target_object = record.get("target_object")
        stored.target_offset = record.get("target_offset", (0.0, 0.0, 0.0))
        stored.pose_source = record.get("pose_source", "CURRENT_POSE")
        stored.source_frame = max(0, int(record["source_frame"]))
        stored.source_frame_count = max(0, int(record["source_frame_count"]))
        stored.target_frame = max(0, int(record["target_frame"]))
        stored.direction = record["direction"]

        if stored.constraint_input in {"POSE", "OBJECT_TARGET"}:
            payload = constraint_utils.get_constraint_payload(record["payload_key"])
            if payload is None:
                raise ValueError("Pose constraint payload is missing")
            constraint_utils.cache_constraint_payload(stored.uid, payload)


def _copy_attempt_constraints(source_attempt, target_attempt, *, num_frames: int) -> None:
    _configure_attempt_constraints(
        target_attempt,
        _attempt_constraint_records(source_attempt),
        num_frames=num_frames,
        text_weight=source_attempt.constraint_text_weight,
        constraint_weight=source_attempt.constraint_weight,
        first_heading=source_attempt.constraint_first_heading,
    )


def _chain_start_frame(
    obj: bpy.types.Object,
    requested_start: int,
    requested_frame_count: int,
    transition_frames: int,
) -> tuple[int, int]:
    latest_strip = _find_latest_strip(obj)
    if latest_strip is None:
        return requested_start, 0

    latest_end = int(round(latest_strip.frame_end_ui))
    requested_start = int(round(requested_start))

    # Only auto-chain when generating from the current tail of the timeline.
    # If the user manually picked a different start frame, respect it.
    if requested_start != latest_end:
        return requested_start, 0

    safe_transition = min(
        max(0, int(transition_frames)),
        max(0, requested_frame_count - 1),
        max(0, _frame_count_from_strip(latest_strip) - 1),
    )
    start_frame = latest_end - safe_transition
    return start_frame, safe_transition


def _move_generation_cursor(settings: AvacapoSettings, frame_end: float) -> None:
    settings.start = int(round(frame_end))


def _tag_ui_redraw(context: bpy.types.Context | None = None) -> None:
    screen = getattr(context, "screen", None) or getattr(bpy.context, "screen", None)
    if screen is None:
        return

    animation_editor_types = {
        "VIEW_3D",
        "DOPESHEET_EDITOR",
        "GRAPH_EDITOR",
        "NLA_EDITOR",
    }
    for area in screen.areas:
        if area.type in animation_editor_types:
            area.tag_redraw()


def _strip_handoff_frame(nla_strip: bpy.types.NlaStrip) -> float:
    return float(nla_strip.frame_start_ui + nla_strip.blend_in)


def _strip_action_frame_at_timeline(nla_strip: bpy.types.NlaStrip, timeline_frame: float) -> float:
    local_frame = float(nla_strip.action_frame_start + (timeline_frame - nla_strip.frame_start_ui))
    return max(
        float(nla_strip.action_frame_start), min(float(nla_strip.action_frame_end), local_frame)
    )


def _find_preceding_strip(
    obj: bpy.types.Object, current_strip: bpy.types.NlaStrip
) -> bpy.types.NlaStrip | None:
    handoff_frame = _strip_handoff_frame(current_strip)
    previous_strip = None
    previous_end = float("-inf")

    for nla_strip in _iter_all_strips(obj):
        if nla_strip == current_strip:
            continue
        if nla_strip.frame_end_ui <= handoff_frame and nla_strip.frame_end_ui > previous_end:
            previous_end = nla_strip.frame_end_ui
            previous_strip = nla_strip

    return previous_strip


def _sorted_strips(obj: bpy.types.Object) -> list[bpy.types.NlaStrip]:
    return sorted(
        _iter_all_strips(obj) or (),
        key=lambda strip: (
            float(strip.frame_start_ui),
            float(strip.frame_end_ui),
            strip.name,
        ),
    )


def _inbetween_source_document(obj: bpy.types.Object):
    animation_data = obj.animation_data
    if animation_data is None:
        return None

    if animation_data.action is not None:
        return bvh_smpl.get_cached_bvh_document(animation_data.action.name)

    actions = []
    for nla_track in animation_data.nla_tracks:
        if nla_track.mute:
            continue
        for nla_strip in nla_track.strips:
            if getattr(nla_strip, "mute", False) or nla_strip.action is None:
                continue
            if nla_strip.action not in actions:
                actions.append(nla_strip.action)

    if not actions:
        return None
    if len(actions) != 1:
        raise ValueError(f"{obj.name} must have exactly one active source clip for Inbetween")

    return bvh_smpl.get_cached_bvh_document(actions[0].name)


def _align_strip_root_motion(obj: bpy.types.Object, nla_strip: bpy.types.NlaStrip) -> None:
    previous_strip = _find_preceding_strip(obj, nla_strip)
    if previous_strip is None or previous_strip.action is None or nla_strip.action is None:
        return

    handoff_frame = _strip_handoff_frame(nla_strip)
    target_frame = _strip_action_frame_at_timeline(previous_strip, handoff_frame)
    source_frame = _strip_action_frame_at_timeline(nla_strip, handoff_frame)
    target_location = animation_utils.evaluate_root_location(
        obj, previous_strip.action, target_frame
    )
    if target_location is None:
        target_location = animation_utils.Vector((0.0, 0.0, 0.0))

    animation_utils.offset_root_location(
        obj,
        nla_strip.action,
        source_frame=source_frame,
        target_location=target_location,
    )


def _realign_following_strips(obj: bpy.types.Object, changed_strip: bpy.types.NlaStrip) -> None:
    seen_changed_strip = False

    for nla_strip in _sorted_strips(obj):
        if nla_strip == changed_strip:
            seen_changed_strip = True
            continue

        if not seen_changed_strip:
            continue

        _align_strip_root_motion(obj, nla_strip)


class AVACAPO_OT_login_browser(bpy.types.Operator):
    """Open browser to create an API token and receive it automatically"""

    bl_idname = "avacapo.login_browser"
    bl_label = "Login via Browser"

    _auth_server = None
    _timer = None

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._auth_server and not self._auth_server.is_running:
            context.window_manager.event_timer_remove(self._timer)

            if self._auth_server.token:
                Storage.api_token = self._auth_server.token
                Storage.save()
                clear_models_cache()
                start_update_check(force=True)
                self.report({"INFO"}, "Connected successfully!")
                log.info("Browser auth completed")
            else:
                self.report({"WARNING"}, "Auth timed out or was cancelled")
                log.warning("Browser auth timed out")

            self._auth_server = None
            # Force UI redraw
            for area in context.screen.areas:
                area.tag_redraw()
            return {"FINISHED"}

        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        from .auth_server import PluginAuthServer

        self._auth_server = PluginAuthServer()
        port = self._auth_server.start()

        # Open the token page with plugin auth params
        url = f"{config.FRONTEND_URL}app/api-tokens?plugin_auth=true&port={port}"
        webbrowser.open(url)
        log.info(f"Opened browser for auth: {url}")

        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, "Waiting for browser auth...")
        return {"RUNNING_MODAL"}


class AVACAPO_OT_paste_token(bpy.types.Operator):
    """Save a manually pasted API token"""

    bl_idname = "avacapo.paste_token"
    bl_label = "Connect"

    def execute(self, context):
        settings = context.scene.avacapo_settings
        token = settings.token_input.strip()
        if not token:
            self.report({"WARNING"}, "Token is empty")
            return {"CANCELLED"}

        Storage.api_token = token
        Storage.save()
        clear_models_cache()
        start_update_check(force=True)
        settings.token_input = ""
        self.report({"INFO"}, "Connected successfully!")
        return {"FINISHED"}


class AVACAPO_OT_disconnect(bpy.types.Operator):
    """Clear the stored API token"""

    bl_idname = "avacapo.disconnect"
    bl_label = "Disconnect"

    def execute(self, context):
        Storage.api_token = ""
        Storage.save()
        clear_models_cache()
        reset_update_check_state()
        _tag_ui_redraw(context)
        self.report({"INFO"}, "Disconnected")
        return {"FINISHED"}


class AVACAPO_OT_fetch(bpy.types.Operator):
    bl_idname = "avacapo.fetch"
    bl_label = "Get Animation"
    bl_description = "Get AI Generated animation"

    _thread = None
    _result = None
    _error = None
    _timer = None
    _request = None

    clip_name: bpy.props.StringProperty()
    attempt_uid: bpy.props.StringProperty()
    obj_name: bpy.props.StringProperty()

    def modal(self, context, event):
        time_start = time.time()
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._thread and not self._thread.is_alive():
            log.debug("Background thread finished, cleaning up timer")
            if self._timer is not None:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            try:
                if self._error:
                    msg = f"Request failed: {self._error}"
                    log.error(msg)
                    self.report({"ERROR"}, msg)
                    return {"CANCELLED"}

                log.info("BVH received: %s bytes", len(self._result))
                try:
                    self._execute(self._result, self.obj_name, self.attempt_uid, self.clip_name)
                except Exception as exc:
                    message = f"Applying animation failed: {exc}"
                    log.exception(message)
                    self.report({"ERROR"}, message)
                    return {"CANCELLED"}

                self.report({"INFO"}, "animation applied")
                time_end = time.time()
                log.debug(f"time of modal operator animation apply is {time_end - time_start}")
                return {"FINISHED"}
            finally:
                State.end_generation()
                _tag_ui_redraw(context)

        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        if State.server_busy:
            self.report({"INFO"}, "Generation already in progress.")
            return {"CANCELLED"}

        attempt = animation_utils.find_attempt(self.obj_name, self.attempt_uid)
        if attempt is None:
            self.report({"ERROR"}, "Generation take was not found.")
            return {"CANCELLED"}

        num_frames = max(1, int(attempt.constraint_num_frames))
        if not attempt.use_constraints and not attempt.constraints and num_frames == 1:
            num_frames = max(1, int(round(attempt.duration * get_fps())))

        pose_constraints = []
        motion_files = []
        direction = None
        for record in _attempt_constraint_records(attempt):
            if record["constraint_input"] == "DIRECTION":
                if direction is not None:
                    self.report({"ERROR"}, "Only one direction constraint is supported.")
                    return {"CANCELLED"}
                direction = list(record["direction"])
                continue

            pose_payload = constraint_utils.get_constraint_payload(record["payload_key"])
            if pose_payload is None:
                self.report(
                    {"ERROR"},
                    "A pose constraint is no longer in memory; create the take again.",
                )
                return {"CANCELLED"}
            pose_constraints.append(
                {
                    "constraint_type": record["constraint_type"],
                    "joint_names": record["joint_names"],
                    "source_frame": record["source_frame"],
                    "target_frame": record["target_frame"],
                }
            )
            motion_files.append(pose_payload)

        self._request = {
            "prompt": str(attempt.prompt),
            "duration": float(attempt.duration),
            "temperature": float(attempt.temperature),
            "num_frames": num_frames,
            "model": str(attempt.model),
            "in_place": bool(attempt.in_place),
            "pose_constraints": pose_constraints,
            "motion_files": motion_files,
            "direction": direction,
            "text_weight": float(attempt.constraint_text_weight),
            "constraint_weight": float(attempt.constraint_weight),
            "first_heading": float(attempt.constraint_first_heading),
        }

        self._time_start = time.time()
        self._result = None
        self._error = None

        self._thread = threading.Thread(target=self._fetch, daemon=True)
        State.begin_generation()
        try:
            self._thread.start()
        except Exception as exc:
            State.end_generation()
            self.report({"ERROR"}, f"Failed to start generation: {exc}")
            return {"CANCELLED"}

        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        _tag_ui_redraw(context)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        State.end_generation()
        _tag_ui_redraw(context)

    def _fetch(self):
        request = self._request
        selected_model = resolve_model_type(request["model"])
        log.debug("Thread started, opening URL...")
        try:
            has_constraints = (
                bool(request["pose_constraints"])
                or request["direction"] is not None
            )
            if has_constraints:
                raw = get_animation_constraints(
                    prompt=request["prompt"],
                    num_frames=request["num_frames"],
                    pose_constraints=request["pose_constraints"],
                    motion_files=request["motion_files"],
                    text_weight=request["text_weight"],
                    constraint_weight=request["constraint_weight"],
                    first_heading=request["first_heading"],
                    direction=request["direction"],
                    model=selected_model,
                    in_place=request["in_place"],
                )
            else:
                raw = get_animation(
                    prompt=request["prompt"],
                    duration=request["duration"],
                    temperature=request["temperature"],
                    model=selected_model,
                    in_place=request["in_place"],
                )
            log.debug("Raw BVH response received: %s bytes", len(raw))
            self._result = raw
        except Exception as e:
            self._error = str(e)
            log.exception("Unexpected error in fetch thread")

    @staticmethod
    def _execute(fetch_result, obj_name, attempt_uid, clip_name):
        settings = bpy.data.scenes[0].avacapo_settings
        attempt = animation_utils.find_attempt(obj_name, attempt_uid)
        action = bpy.data.actions[attempt.action_name]
        obj = bpy.data.objects[obj_name]
        if not action:
            log.debug("no action found")
        else:
            document = bvh_smpl.cache_bvh_document(action.name, fetch_result)
            animation_utils.apply_animation(obj, document, action)
            nla_strip = obj.animation_data.nla_tracks[clip_name].strips[clip_name]
            _sync_strip_to_action(nla_strip, action)
            _align_strip_root_motion(obj, nla_strip)
            _realign_following_strips(obj, nla_strip)
            _move_generation_cursor(settings, nla_strip.frame_end_ui)
            obj.animation_data.action = None


class AVACAPO_OT_generate_inbetween(bpy.types.Operator):
    bl_idname = "avacapo.generate_inbetween"
    bl_label = "Generate Inbetween"
    bl_description = "Join two animations with an AI-generated transition"
    bl_options = {"REGISTER", "UNDO"}

    _thread = None
    _result = None
    _error = None
    _timer = None
    _request = None
    _left_document = None
    _right_document = None
    _source_mode = "ARMATURES"
    _timeline_action_name = None
    _timeline_left_frame = None
    _timeline_right_frame = None

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._thread and not self._thread.is_alive():
            if self._timer is not None:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            try:
                if self._error:
                    message = f"Inbetween request failed: {self._error}"
                    log.error(message)
                    self.report({"ERROR"}, message)
                    return {"CANCELLED"}

                try:
                    total_frames = self._apply_result(context)
                except Exception as exc:
                    message = f"Applying inbetween failed: {exc}"
                    log.exception(message)
                    self.report({"ERROR"}, message)
                    return {"CANCELLED"}

                self.report({"INFO"}, f"Inbetween applied: {total_frames} frames")
                return {"FINISHED"}
            finally:
                State.end_generation()
                _tag_ui_redraw(context)

        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        if State.server_busy:
            self.report({"INFO"}, "Generation already in progress.")
            return {"CANCELLED"}

        settings = context.scene.avacapo_settings
        self._source_mode = str(settings.inbetween_source_mode)
        self._left_document = None
        self._right_document = None
        self._timeline_action_name = None
        self._timeline_left_frame = None
        self._timeline_right_frame = None

        if self._source_mode == "TIMELINE":
            prepared = self._prepare_timeline_request(context, settings)
        else:
            prepared = self._prepare_armature_request(context, settings)
        if prepared is None:
            return {"CANCELLED"}

        left_payload, right_payload, gap_frames, request_name = prepared
        self._request = {
            "left_context_pose": left_payload,
            "right_context_pose": right_payload,
            "name": request_name,
            "prompt": str(settings.prompt),
            "left_frame": 0,
            "right_frame": 0,
            "left_context_frames": 1,
            "right_context_frames": 1,
            "inbetween_frames": gap_frames,
            "model": resolve_model_type(settings.model),
            "in_place": bool(settings.in_place),
            # Existing animation keeps its heading on the right side. Rotating
            # only the generated gap would introduce a visible discontinuity.
            "align_heading": False,
        }
        self._result = None
        self._error = None
        self._thread = threading.Thread(target=self._fetch, daemon=True)

        State.begin_generation()
        try:
            self._thread.start()
        except Exception as exc:
            State.end_generation()
            self.report({"ERROR"}, f"Failed to start inbetween generation: {exc}")
            return {"CANCELLED"}

        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        _tag_ui_redraw(context)
        return {"RUNNING_MODAL"}

    def _prepare_armature_request(self, context, settings):
        left_obj = settings.inbetween_left_armature
        right_obj = settings.inbetween_right_armature
        if left_obj is None or right_obj is None:
            self.report({"ERROR"}, "Select both source armatures")
            return None
        if left_obj == right_obj:
            self.report({"ERROR"}, "First and Second Armature must be different")
            return None
        if rig_utils.infer_rig_type(left_obj) == "unknown":
            self.report({"ERROR"}, "First Armature must use an AvaCapo or Mixamo rig")
            return None
        if rig_utils.infer_rig_type(right_obj) == "unknown":
            self.report({"ERROR"}, "Second Armature must use an AvaCapo or Mixamo rig")
            return None

        gap_frames = int(round(float(settings.duration) * get_fps()))
        if gap_frames <= 0:
            self.report({"ERROR"}, "Inbetween duration must be positive")
            return None

        try:
            self._left_document = _inbetween_source_document(left_obj)
            self._right_document = _inbetween_source_document(right_obj)
            if self._left_document is None:
                left_payload, _frame_count = constraint_utils.create_pose_constraint_npz(
                    context,
                    left_obj,
                    "CURRENT_POSE",
                )
            else:
                left_payload = bvh_smpl.create_smpl_npz_bytes(
                    bvh_smpl.convert_bvh_smpl(
                        self._left_document,
                        start_frame=self._left_document.frame_count - 1,
                        end_frame=self._left_document.frame_count,
                        target_fps=get_fps(),
                    )
                )
            if self._right_document is None:
                right_payload, _frame_count = constraint_utils.create_pose_constraint_npz(
                    context,
                    right_obj,
                    "CURRENT_POSE",
                )
            else:
                right_payload = bvh_smpl.create_smpl_npz_bytes(
                    bvh_smpl.convert_bvh_smpl(
                        self._right_document,
                        start_frame=0,
                        end_frame=1,
                        target_fps=get_fps(),
                    )
                )
        except Exception as exc:
            message = f"Preparing Inbetween input failed: {exc}"
            log.exception(message)
            self.report({"ERROR"}, message)
            return None

        self._left_obj_name = left_obj.name
        self._right_obj_name = right_obj.name
        return (
            left_payload,
            right_payload,
            gap_frames,
            f"inbetween_{left_obj.name}_{right_obj.name}",
        )

    def _prepare_timeline_request(self, context, settings):
        obj = context.object
        if obj is None or obj.type != "ARMATURE":
            self.report({"ERROR"}, "Select the armature with the keyframes")
            return None
        if rig_utils.infer_rig_type(obj) == "unknown":
            self.report({"ERROR"}, "Active Armature must use an AvaCapo or Mixamo rig")
            return None

        action, selected_frames = animation_utils.selected_action_keyframe_frames(obj)
        if action is None:
            self.report({"ERROR"}, "The active armature has no active Action")
            return None
        if action.library is not None:
            self.report({"ERROR"}, "The active Action is linked and cannot be edited")
            return None
        if len(selected_frames) != 2:
            self.report({"ERROR"}, "Select keyframes on exactly two timeline frames")
            return None
        if any(abs(frame - round(frame)) > 1e-6 for frame in selected_frames):
            self.report({"ERROR"}, "Selected keyframes must be on whole timeline frames")
            return None

        left_frame, right_frame = (int(round(frame)) for frame in selected_frames)
        gap_frames = right_frame - left_frame - 1
        if gap_frames <= 0:
            self.report({"ERROR"}, "Leave at least one empty frame between the two keys")
            return None
        if not animation_utils.bracketed_pose_curve_count(
            obj,
            action,
            left_frame,
            right_frame,
        ):
            self.report({"ERROR"}, "The anchor frames must key the same pose channel")
            return None

        try:
            left_payload, _frame_count = constraint_utils.create_pose_constraint_npz_at_frames(
                context,
                obj,
                [left_frame],
            )
            right_payload, _frame_count = constraint_utils.create_pose_constraint_npz_at_frames(
                context,
                obj,
                [right_frame],
            )
        except Exception as exc:
            message = f"Preparing timeline Inbetween input failed: {exc}"
            log.exception(message)
            self.report({"ERROR"}, message)
            return None

        self._left_obj_name = obj.name
        self._right_obj_name = None
        self._timeline_action_name = action.name
        self._timeline_left_frame = left_frame
        self._timeline_right_frame = right_frame
        return (
            left_payload,
            right_payload,
            gap_frames,
            f"inbetween_{obj.name}_{left_frame}_{right_frame}",
        )

    def cancel(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        State.end_generation()
        _tag_ui_redraw(context)

    def _fetch(self):
        try:
            self._result = get_animation_inbetween(**self._request)
        except Exception as exc:
            self._error = str(exc)
            log.exception("Unexpected error in inbetween fetch thread")

    def _apply_result(self, context) -> int:
        if self._source_mode == "TIMELINE":
            return self._apply_timeline_result(context)

        left_obj = bpy.data.objects.get(self._left_obj_name)
        if left_obj is None:
            raise ValueError("First Armature was removed while generating")

        gap_document = bvh_smpl.load_bvh_document_from_bytes(self._result)
        combined_document = bvh_smpl.concatenate_bvh_documents(
            *(
                document
                for document in (
                    self._left_document,
                    gap_document,
                    self._right_document,
                )
                if document is not None
            ),
            align_root_translation=True,
        )

        action = bpy.data.actions.new(
            name=f"Inbetween_{self._left_obj_name}_{self._right_obj_name}"
        )
        action.slots.new(left_obj.id_type, name=left_obj.name)
        animation_data = left_obj.animation_data_create()
        previous_action = animation_data.action
        track_mute_states = [track.mute for track in animation_data.nla_tracks]
        for nla_track in animation_data.nla_tracks:
            nla_track.mute = True

        try:
            animation_utils.apply_animation(left_obj, combined_document, action)
            bvh_smpl.cache_bvh_document(action.name, combined_document)
            animation_data.action = action
        except Exception:
            animation_data.action = previous_action
            for nla_track, was_muted in zip(animation_data.nla_tracks, track_mute_states):
                nla_track.mute = was_muted
            bpy.data.actions.remove(action)
            raise

        context.scene.frame_set(1)
        context.scene.frame_end = max(int(context.scene.frame_end), combined_document.frame_count)
        left_obj.select_set(True)
        context.view_layer.objects.active = left_obj

        right_obj = bpy.data.objects.get(self._right_obj_name)
        if right_obj is not None:
            right_armature = right_obj.data
            bpy.data.objects.remove(right_obj, do_unlink=True)
            if right_armature is not None and right_armature.users == 0:
                bpy.data.armatures.remove(right_armature)
        return combined_document.frame_count

    def _apply_timeline_result(self, context) -> int:
        obj = bpy.data.objects.get(self._left_obj_name)
        if obj is None:
            raise ValueError("Timeline armature was removed while generating")
        action = bpy.data.actions.get(self._timeline_action_name)
        if action is None:
            raise ValueError("Timeline Action was removed while generating")

        gap_document = bvh_smpl.load_bvh_document_from_bytes(self._result)
        if gap_document.frame_count <= 0:
            raise ValueError("The generated Inbetween contains no frames")

        temporary_action = bpy.data.actions.new(
            name=f"__avacapo_timeline_inbetween_{uuid.uuid4().hex[:8]}"
        )
        temporary_action.slots.new(obj.id_type, name=obj.name)
        animation_data = obj.animation_data_create()
        previous_action = animation_data.action
        track_mute_states = [track.mute for track in animation_data.nla_tracks]
        previous_frame = int(context.scene.frame_current)

        try:
            try:
                for nla_track in animation_data.nla_tracks:
                    nla_track.mute = True
                animation_utils.apply_animation(obj, gap_document, temporary_action)
            finally:
                animation_data.action = previous_action
                for nla_track, was_muted in zip(
                    animation_data.nla_tracks, track_mute_states
                ):
                    nla_track.mute = was_muted
                context.scene.frame_set(previous_frame)
                context.view_layer.update()

            inserted_frames = animation_utils.insert_action_range(
                obj,
                action,
                temporary_action,
                target_frame_start=self._timeline_left_frame + 1,
                target_frame_end=self._timeline_right_frame - 1,
                source_frame_count=gap_document.frame_count,
            )
            bvh_smpl.invalidate_cached_action(action.name)
        finally:
            bpy.data.actions.remove(temporary_action)

        # Make the edited Action visible even if Blender detached or switched
        # it while the asynchronous server request was running.
        animation_data.action = action
        action_slot = animation_utils._action_slot_for_object(action, obj)
        if animation_data.action_slot != action_slot:
            animation_data.action_slot = action_slot
        action.update_tag()

        context.scene.frame_end = max(
            int(context.scene.frame_end),
            int(self._timeline_right_frame),
        )
        context.scene.frame_set(int(self._timeline_left_frame))
        context.view_layer.update()
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return inserted_frames


class AVACAPO_OT_convert_smpl_preview_range(bpy.types.Operator):
    """Convert the timeline preview range to an in-memory SMPL-X NPZ payload"""

    bl_idname = "avacapo.convert_smpl_preview_range"
    bl_label = "Convert Preview Range"
    bl_description = "Convert this take's Timeline Preview Range to SMPL-X in memory"

    clip_name: bpy.props.StringProperty()
    attempt_uid: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        obj = context.object
        if obj is None:
            self.report({"ERROR"}, "Select the armature containing this clip")
            return {"CANCELLED"}
        if not scene.use_preview_range:
            self.report({"ERROR"}, "Set a Timeline Preview Range first (press P)")
            return {"CANCELLED"}

        clip = _find_clip_by_name(obj, self.clip_name)
        attempt = animation_utils.find_attempt(obj.name, self.attempt_uid)
        if clip is None or attempt is None:
            self.report({"ERROR"}, "Clip take not found")
            return {"CANCELLED"}
        if clip.active_attempt != attempt.uid:
            self.report({"ERROR"}, "Select this take before converting it")
            return {"CANCELLED"}

        nla_strip = _get_strip_for_clip(obj, clip)
        action = bpy.data.actions.get(attempt.action_name)
        if nla_strip is None or action is None:
            self.report({"ERROR"}, "Clip action not found")
            return {"CANCELLED"}

        visible_start = int(math.ceil(nla_strip.frame_start_ui))
        visible_end = int(math.ceil(nla_strip.frame_end_ui)) - 1
        timeline_start = max(int(scene.frame_preview_start), visible_start)
        timeline_end = min(int(scene.frame_preview_end), visible_end)
        if timeline_end < timeline_start:
            self.report({"ERROR"}, "Preview Range does not overlap this clip")
            return {"CANCELLED"}

        action_start = _strip_action_frame_at_timeline(nla_strip, timeline_start)
        action_end = _strip_action_frame_at_timeline(nla_strip, timeline_end)
        source_start = max(0, int(round(action_start)) - 1)
        source_end = int(round(action_end))

        try:
            conversion = bvh_smpl.convert_cached_action_range(
                action.name,
                start_frame=source_start,
                end_frame=source_end,
            )
        except Exception as exc:
            message = f"SMPL-X conversion failed: {exc}"
            log.exception(message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        size_kib = len(conversion) / 1024.0
        self.report(
            {"INFO"},
            f"Converted  {size_kib:.1f} KiB in memory",
        )
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_begin_constraint(bpy.types.Operator):
    bl_idname = "avacapo.begin_constraint"
    bl_label = "Add Constraint"

    def execute(self, context):
        settings = context.scene.avacapo_settings
        settings.constraint_edit_index = -1
        settings.show_constraint_editor = True
        settings.show_constraint_settings = True
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_cancel_constraint(bpy.types.Operator):
    bl_idname = "avacapo.cancel_constraint"
    bl_label = "Cancel Constraint"

    def execute(self, context):
        settings = context.scene.avacapo_settings
        settings.constraint_edit_index = -1
        settings.show_constraint_editor = False
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_edit_constraint(bpy.types.Operator):
    bl_idname = "avacapo.edit_constraint"
    bl_label = "Edit Constraint"

    constraint_index: bpy.props.IntProperty()

    def execute(self, context):
        settings = context.scene.avacapo_settings
        if not 0 <= self.constraint_index < len(settings.constraints):
            self.report({"ERROR"}, "Constraint not found")
            return {"CANCELLED"}

        constraint = settings.constraints[self.constraint_index]
        settings.constraint_input = constraint.constraint_input
        settings.constraint_type = constraint.constraint_type
        settings.constraint_joint_name = set(constraint.constraint_joint_name)
        object_target_joint_names = constraint_utils.selected_joint_names(
            constraint.constraint_joint_name
        )
        if (
            object_target_joint_names
            and object_target_joint_names[0]
            in constraint_utils.OBJECT_TARGET_CONSTRAINT_TYPES
        ):
            settings.constraint_object_target_joint = object_target_joint_names[0]
        settings.constraint_source_armature = constraint.source_armature
        settings.constraint_target_object = constraint.target_object
        settings.constraint_target_offset = constraint.target_offset
        settings.constraint_pose_source = constraint.pose_source
        settings.constraint_source_frame = constraint.source_frame
        settings.constraint_target_frame = constraint.target_frame
        settings.constraint_direction = constraint.direction
        settings.constraint_edit_index = self.constraint_index
        settings.show_constraint_editor = True
        settings.show_constraint_settings = True
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_remove_constraint(bpy.types.Operator):
    bl_idname = "avacapo.remove_constraint"
    bl_label = "Remove Constraint"

    constraint_index: bpy.props.IntProperty()

    def execute(self, context):
        settings = context.scene.avacapo_settings
        if not 0 <= self.constraint_index < len(settings.constraints):
            self.report({"ERROR"}, "Constraint not found")
            return {"CANCELLED"}

        constraint_utils.remove_constraint_payload(settings.constraints[self.constraint_index].uid)
        settings.constraints.remove(self.constraint_index)
        settings.constraint_edit_index = -1
        settings.show_constraint_editor = False
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_clear_constraints(bpy.types.Operator):
    bl_idname = "avacapo.clear_constraints"
    bl_label = "Clear Constraints"

    def execute(self, context):
        settings = context.scene.avacapo_settings
        for constraint in settings.constraints:
            constraint_utils.remove_constraint_payload(constraint.uid)
        settings.constraints.clear()
        settings.constraint_edit_index = -1
        settings.show_constraint_editor = False
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_save_constraint(bpy.types.Operator):
    bl_idname = "avacapo.save_constraint"
    bl_label = "Save Constraint"

    def execute(self, context):
        settings = context.scene.avacapo_settings
        validation_error = constraint_utils.validate_constraint_settings(context, settings)
        if validation_error:
            self.report({"ERROR"}, validation_error)
            return {"CANCELLED"}

        edit_index = int(settings.constraint_edit_index)
        if settings.constraint_input == "DIRECTION":
            for index, constraint in enumerate(settings.constraints):
                if index != edit_index and constraint.constraint_input == "DIRECTION":
                    self.report({"ERROR"}, "Only one direction constraint can be added")
                    return {"CANCELLED"}

        source = None
        pose_payload = None
        source_frame_count = 0
        source_frame = int(settings.constraint_source_frame)
        constraint_type = settings.constraint_type
        joint_names = constraint_utils.selected_joint_names(settings.constraint_joint_name)
        if settings.constraint_input == "POSE":
            source = constraint_utils.source_armature(context, settings)
            try:
                pose_payload, source_frame_count = constraint_utils.create_pose_constraint_npz(
                    context,
                    source,
                    settings.constraint_pose_source,
                )
            except Exception as exc:
                message = f"Pose constraint conversion failed: {exc}"
                log.exception(message)
                self.report({"ERROR"}, message)
                return {"CANCELLED"}
            if source_frame_count == 1:
                source_frame = 0
        elif settings.constraint_input == "OBJECT_TARGET":
            source = constraint_utils.source_armature(context, settings)
            try:
                joint_name = constraint_utils.object_target_joint_name(
                    settings.constraint_object_target_joint
                )
                joint_names = [joint_name]
                constraint_type = constraint_utils.object_target_constraint_type(joint_name)
                pose_payload, source_frame_count = (
                    constraint_utils.create_object_target_constraint_npz(
                        context,
                        source,
                        settings.constraint_target_object,
                        joint_name,
                        settings.constraint_target_offset,
                    )
                )
            except Exception as exc:
                message = f"Object-target IK conversion failed: {exc}"
                log.exception(message)
                self.report({"ERROR"}, message)
                return {"CANCELLED"}
            source_frame = 0

        if 0 <= edit_index < len(settings.constraints):
            stored = settings.constraints[edit_index]
        else:
            stored = settings.constraints.add()
            stored.uid = uuid.uuid4().hex

        stored.constraint_input = settings.constraint_input
        stored.constraint_type = constraint_type
        stored.constraint_joint_name = set(joint_names)
        stored.source_armature = source
        stored.target_object = (
            settings.constraint_target_object
            if settings.constraint_input == "OBJECT_TARGET"
            else None
        )
        stored.target_offset = (
            settings.constraint_target_offset
            if settings.constraint_input == "OBJECT_TARGET"
            else (0.0, 0.0, 0.0)
        )
        stored.pose_source = (
            "CURRENT_POSE"
            if settings.constraint_input == "OBJECT_TARGET"
            else settings.constraint_pose_source
        )
        stored.source_frame = source_frame
        stored.source_frame_count = source_frame_count
        stored.target_frame = int(settings.constraint_target_frame)
        stored.direction = settings.constraint_direction

        if pose_payload is not None:
            constraint_utils.cache_constraint_payload(stored.uid, pose_payload)
        else:
            constraint_utils.remove_constraint_payload(stored.uid)

        settings.constraint_edit_index = -1
        settings.show_constraint_editor = False
        _tag_ui_redraw(context)
        return {"FINISHED"}


class AVACAPO_OT_add_clip(bpy.types.Operator):
    bl_idname = "avacapo.add_clip"
    bl_label = "Add new clip"

    prompt: bpy.props.StringProperty()
    start: bpy.props.IntProperty()
    end: bpy.props.IntProperty()
    transition: bpy.props.IntProperty()
    fadein: bpy.props.IntProperty()
    fadeout: bpy.props.IntProperty()
    model: bpy.props.StringProperty()
    in_place: bpy.props.BoolProperty()

    def execute(self, context):
        if State.server_busy:
            self.report({"INFO"}, "Generation already in progress.")
            return {"CANCELLED"}

        obj = context.object
        settings = context.scene.avacapo_settings
        requested_frame_count = _frame_count_from_bounds(self.start, self.end)
        if settings.show_constraint_editor:
            self.report({"ERROR"}, "Save or cancel the current constraint")
            return {"CANCELLED"}
        validation_error = constraint_utils.validate_saved_constraints(settings)
        if validation_error:
            self.report({"ERROR"}, validation_error)
            return {"CANCELLED"}

        constraint_records = [_constraint_record(constraint) for constraint in settings.constraints]

        obj.animation_data_create()

        existing_clip, existing_strip = _find_clip_at_exact_range(obj, self.start, self.end)
        if existing_clip is not None and existing_strip is not None:
            existing_clip.prompt = self.prompt
            existing_clip.model = self.model
            existing_clip.in_place = self.in_place

            new_attempt, action, slot = _create_attempt_for_clip(
                obj,
                existing_clip,
                prompt=self.prompt,
                model=self.model,
                in_place=self.in_place,
                duration=requested_frame_count / get_fps(),
            )
            _configure_attempt_constraints(
                new_attempt,
                constraint_records,
                num_frames=requested_frame_count,
                text_weight=settings.constraint_text_weight,
                constraint_weight=settings.constraint_weight,
                first_heading=settings.constraint_first_heading,
            )
            existing_strip.action = action
            existing_strip.action_slot = slot
            _move_generation_cursor(settings, existing_strip.frame_end_ui)

            bpy.ops.avacapo.fetch(
                "INVOKE_DEFAULT",
                clip_name=existing_clip.name,
                attempt_uid=new_attempt.uid,
                obj_name=obj.name,
            )
            return {"FINISHED"}

        # create the clip props
        new_clip = obj.avacapo_clips.clips.add()
        new_clip.prompt = self.prompt
        new_clip.name = new_clip.create_name()
        new_clip.model = self.model
        new_clip.in_place = self.in_place

        # generate new attempt
        clip_start, clip_transition = _chain_start_frame(
            obj,
            requested_start=self.start,
            requested_frame_count=requested_frame_count,
            transition_frames=self.transition,
        )

        new_attempt, action, slot = _create_attempt_for_clip(
            obj,
            new_clip,
            prompt=self.prompt,
            model=self.model,
            in_place=self.in_place,
            duration=requested_frame_count / get_fps(),
        )
        _configure_attempt_constraints(
            new_attempt,
            constraint_records,
            num_frames=requested_frame_count,
            text_weight=settings.constraint_text_weight,
            constraint_weight=settings.constraint_weight,
            first_heading=settings.constraint_first_heading,
        )

        # add the attempt to queue and start fetching it
        # GlobalQueue.add_attempt(new_attempt.uid)

        # bind nla
        nla_tracks = obj.animation_data.nla_tracks
        nla_track = nla_tracks.new(prev=None)
        nla_track.name = new_clip.name
        nla_strip = nla_track.strips.new(
            name=new_clip.name,
            start=clip_start,
            action=action,
        )

        nla_strip.action_slot = slot
        _configure_strip_timing(
            nla_strip,
            start_frame=clip_start,
            frame_count=requested_frame_count,
            blend_in=clip_transition,
            blend_out=0,
        )
        _move_generation_cursor(settings, nla_strip.frame_end_ui)

        bpy.ops.avacapo.fetch(
            "INVOKE_DEFAULT",
            clip_name=new_clip.name,
            attempt_uid=new_attempt.uid,
            obj_name=obj.name,
        )

        return {"FINISHED"}


class AVACAPO_OT_new_attempt(bpy.types.Operator):
    bl_idname = "avacapo.new_attempt"
    bl_label = "Select objct by name"
    bl_options = {"REGISTER", "UNDO"}

    clip_uid: bpy.props.StringProperty()

    def execute(self, context):
        if State.server_busy:
            self.report({"INFO"}, "Generation already in progress.")
            return {"CANCELLED"}

        obj = context.object
        clip = _find_clip_by_name(obj, self.clip_uid)
        if clip is None:
            self.report({"ERROR"}, "Clip not found.")
            return {"CANCELLED"}

        nla_strip = _get_strip_for_clip(obj, clip)
        if nla_strip is None:
            self.report({"ERROR"}, "Clip strip not found.")
            return {"CANCELLED"}

        source_attempt = next(
            (attempt for attempt in clip.attempts if attempt.uid == clip.active_attempt),
            None,
        )
        if source_attempt is None:
            self.report({"ERROR"}, "Active take not found.")
            return {"CANCELLED"}
        source_constraint_records = _attempt_constraint_records(source_attempt)
        if any(
            record["constraint_input"] in {"POSE", "OBJECT_TARGET"}
            and constraint_utils.get_constraint_payload(record["payload_key"]) is None
            for record in source_constraint_records
        ):
            self.report(
                {"ERROR"},
                "A pose constraint is no longer in memory; create a new constrained clip.",
            )
            return {"CANCELLED"}

        frame_count = _frame_count_from_bounds(nla_strip.frame_start_ui, nla_strip.frame_end_ui)

        new_attempt, action, slot = _create_attempt_for_clip(
            obj,
            clip,
            prompt=clip.prompt,
            model=clip.model,
            in_place=clip.in_place,
            duration=frame_count / get_fps(),
        )
        _copy_attempt_constraints(source_attempt, new_attempt, num_frames=frame_count)

        nla_strip.action = action
        nla_strip.action_slot = slot

        bpy.ops.avacapo.fetch(
            "INVOKE_DEFAULT",
            clip_name=clip.name,
            attempt_uid=new_attempt.uid,
            obj_name=obj.name,
        )

        return {"FINISHED"}


class AVACAPO_OT_SelectAttempt(bpy.types.Operator):
    bl_idname = "avacapo.select_attempt"
    bl_label = "Select Attempt"
    bl_description = "Switch the clip to this generated take"

    attempt_uid: bpy.props.StringProperty()
    clip_uid: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.object
        attempt = animation_utils.find_attempt(obj.name, self.attempt_uid)
        clip = _find_clip_by_name(obj, self.clip_uid)
        if attempt is None or clip is None:
            self.report({"ERROR"}, "Attempt not found.")
            return {"CANCELLED"}

        clip.active_attempt = self.attempt_uid

        nla_strip = _get_strip_for_clip(obj, clip)
        if nla_strip is None:
            self.report({"ERROR"}, "Clip strip not found.")
            return {"CANCELLED"}

        action = bpy.data.actions[attempt.action_name]
        nla_strip.action = action
        nla_strip.action_slot = action.slots[f"OB{obj.name}"]
        _sync_strip_to_action(nla_strip, action)
        _align_strip_root_motion(obj, nla_strip)
        _realign_following_strips(obj, nla_strip)

        return {"FINISHED"}


class AVACAPO_OT_select_by_name(bpy.types.Operator):
    """select object by name"""

    bl_idname = "avacapo.select_by_name"
    bl_label = "Select objct by name"
    bl_options = {"REGISTER", "UNDO"}
    obj_name: bpy.props.StringProperty()

    def execute(self, context):
        bpy.ops.object.select_all(action="DESELECT")
        bpy.data.objects[self.obj_name].select_set(True)
        bpy.context.view_layer.objects.active = bpy.data.objects[self.obj_name]
        return {"FINISHED"}


class AVACAPO_OT_reset_import_pose(bpy.types.Operator):
    """Reset imported armature pose to its rest pose"""

    bl_idname = "avacapo.reset_import_pose"
    bl_label = "Reset Imported Pose"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        if obj is None or obj.type != "ARMATURE":
            self.report({"ERROR"}, "Select an armature first.")
            return {"CANCELLED"}

        if rig_utils.infer_rig_type(obj) != "mixamo":
            self.report({"ERROR"}, "This action is only available for Mixamo rigs.")
            return {"CANCELLED"}

        animation_utils.reset_pose_transforms(obj)
        self.report({"INFO"}, "Mixamo rig reset to rest pose.")
        return {"FINISHED"}


class AVACAPO_OT_create_avacapo_v1(bpy.types.Operator):
    """Create a new armature preset"""

    bl_idname = "avacapo.create_avacapo_v1"
    bl_label = "Create Armature"
    bl_options = {"REGISTER", "UNDO"}

    armature_preset: bpy.props.EnumProperty(
        name="Armature",
        description="Armature preset to create",
        items=[
            ("avacapo", "AvaCapo", "Append the bundled AvaCapo rig"),
            ("mixamo", "Mixamo", "Import the bundled Mixamo BVH rig"),
        ],
        default="avacapo",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "armature_preset", expand=True)

    def _select_created_object(self, context, obj: bpy.types.Object, success_message: str):
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({"INFO"}, success_message)
        return {"FINISHED"}

    def _append_avacapo_rig(self, context):
        inner_path = "Object"
        object_name = config.AVACAPO_RIG_NAME
        existing_object_names = set(bpy.data.objects.keys())

        bpy.ops.wm.append(
            filepath=os.path.join(config.BLEND_PATH, inner_path, object_name),
            directory=os.path.join(config.BLEND_PATH, inner_path),
            filename=object_name,
        )

        created_armatures = [
            obj
            for obj in bpy.data.objects
            if obj.name not in existing_object_names and obj.type == "ARMATURE"
        ]
        if created_armatures:
            return self._select_created_object(
                context,
                created_armatures[-1],
                "AvaCapo rig created!",
            )

        self.report({"WARNING"}, f"Object '{object_name}' was not created after append")
        return {"CANCELLED"}

    def _import_mixamo_rig(self, context):
        if not os.path.exists(config.MIXAMO_BVH_PATH):
            self.report({"ERROR"}, f"Mixamo BVH not found: {config.MIXAMO_BVH_PATH}")
            return {"CANCELLED"}

        existing_object_names = set(bpy.data.objects.keys())
        existing_action_names = set(bpy.data.actions.keys())

        bpy.ops.import_anim.bvh(
            filepath=config.MIXAMO_BVH_PATH,
            update_scene_fps=False,
            update_scene_duration=False,
        )

        created_armatures = [
            obj
            for obj in bpy.data.objects
            if obj.name not in existing_object_names and obj.type == "ARMATURE"
        ]
        if not created_armatures:
            self.report({"WARNING"}, "Mixamo armature was not created from BVH import")
            return {"CANCELLED"}

        obj = created_armatures[-1]
        obj.name = "mixamo"
        if obj.data is not None:
            obj.data.name = f"{obj.name}_data"

        created_actions = [
            action for action in bpy.data.actions if action.name not in existing_action_names
        ]
        if obj.animation_data is not None:
            obj.animation_data.action = None
        animation_utils.reset_pose_transforms(obj)
        for action in created_actions:
            if action.users == 0:
                bpy.data.actions.remove(action)

        return self._select_created_object(context, obj, "Mixamo rig created!")

    def execute(self, context):
        if self.armature_preset == "mixamo":
            return self._import_mixamo_rig(context)

        return self._append_avacapo_rig(context)


class AVACAPO_OT_reload_addon(bpy.types.Operator):
    """Reload current addon after update"""

    bl_idname = "avacapo.reload_addon"
    bl_label = "Reload Addon"
    bl_options = {"REGISTER"}

    def execute(self, context):
        bpy.ops.script.reload()
        self.report({"INFO"}, "addon reloaded!")
        return {"FINISHED"}


class AVACAPO_OT_toggle_start_record_lock(bpy.types.Operator):
    """when moving the frame in timeline this start frame will be syncronized"""

    bl_idname = "avacapo.toggle_start_record_lock"
    bl_label = "Lock Start Frame to current frame"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.avacapo_settings
        settings.start_record_lock = not (settings.start_record_lock)
        self.report(
            {"INFO"}, f"start record lock in {'on' if settings.start_record_lock else 'off'}!"
        )
        return {"FINISHED"}


class AVACAPO_OT_create_avacapo(bpy.types.Operator):
    """Not yet implemented"""

    bl_idname = "avacapo.create_avacapo"
    bl_label = "Create avacapo"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        self.report({"INFO"}, "avacapo created!")
        return {"FINISHED"}


class AVACAPO_OT_OpenTextPopover(bpy.types.Operator):
    bl_idname = "avacapo.open_text_popover"
    bl_label = "Preview Text"

    info_str: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=600)

    def draw(self, context):
        layout = self.layout
        box = layout.box()

        lines = textwrap.wrap(self.info_str, width=200)
        for line in lines:
            box.label(text=line)

    def execute(self, context):
        return {"FINISHED"}


# Registration
# --------------------------------------------------------------------


@persistent
def _migrate_generation_mode(_unused=None) -> None:
    scenes = getattr(bpy.data, "scenes", None)
    if scenes is None:
        return None

    for scene in scenes:
        settings = getattr(scene, "avacapo_settings", None)
        if settings is None:
            continue
        try:
            mode = settings.generation_mode
        except (TypeError, ValueError):
            mode = ""
        if mode not in {"STANDARD", "INBETWEEN"}:
            settings.generation_mode = "STANDARD"
    return None


_classes = [
    AvacapoConstraint,
    AvacapoSettings,
    AvacapoAttempt,
    AvacapoClip,
    AvacapoClips,
    AVACAPO_OT_login_browser,
    AVACAPO_OT_paste_token,
    AVACAPO_OT_disconnect,
    AVACAPO_OT_reload_addon,
    AVACAPO_OT_begin_constraint,
    AVACAPO_OT_cancel_constraint,
    AVACAPO_OT_edit_constraint,
    AVACAPO_OT_remove_constraint,
    AVACAPO_OT_clear_constraints,
    AVACAPO_OT_save_constraint,
    AVACAPO_OT_add_clip,
    AVACAPO_OT_new_attempt,
    AVACAPO_OT_fetch,
    AVACAPO_OT_generate_inbetween,
    AVACAPO_OT_convert_smpl_preview_range,
    AVACAPO_OT_create_avacapo,
    AVACAPO_OT_create_avacapo_v1,
    AVACAPO_OT_select_by_name,
    AVACAPO_OT_reset_import_pose,
    AVACAPO_OT_toggle_start_record_lock,
    AVACAPO_OT_OpenTextPopover,
    AVACAPO_OT_SelectAttempt,
    AVACAPO_PT_main_panel,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.avacapo_settings = bpy.props.PointerProperty(type=AvacapoSettings)
    bpy.types.Object.avacapo_clips = bpy.props.PointerProperty(type=AvacapoClips)
    bpy.app.handlers.frame_change_post.append(frame_change_post)
    bpy.app.handlers.depsgraph_update_post.append(rig_utils._init_bone_trees_once)
    if _migrate_generation_mode not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_migrate_generation_mode)
    if not bpy.app.timers.is_registered(_migrate_generation_mode):
        bpy.app.timers.register(_migrate_generation_mode, first_interval=0.0)
    start_update_check(force=True)


def unregister() -> None:
    bvh_smpl.clear_memory_cache()
    constraint_utils.clear_constraint_payloads()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Object.avacapo_clips
    del bpy.types.Scene.avacapo_settings
    bpy.app.handlers.frame_change_post.remove(frame_change_post)
    if rig_utils._init_bone_trees_once in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(rig_utils._init_bone_trees_once)
    if _migrate_generation_mode in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_generation_mode)
    if bpy.app.timers.is_registered(_migrate_generation_mode):
        bpy.app.timers.unregister(_migrate_generation_mode)
