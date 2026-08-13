import bpy
import math
import os
import threading
import time
import webbrowser
import uuid
import textwrap

from .state_controller import State, frame_change_post
from .server import get_animation, get_animation_constraints, get_fps
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
            f"Target Frame {requested} is outside 0..{max_frame}; "
            f"using {max_frame}"
        )
        requested = max_frame
    else:
        settings.constraint_target_error = ""
    settings[_TARGET_FRAME_VALUE_KEY] = requested


def _revalidate_constraint_target(settings) -> None:
    current = int(settings.get(_TARGET_FRAME_VALUE_KEY, 0))
    _constraint_target_frame_set(settings, current)


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
        description="Generate from text only or add a motion constraint",
        items=(
            ("STANDARD", "Standard", "Generate motion from the text prompt"),
            ("CONSTRAINTS", "Constraints", "Generate motion with a pose or direction constraint"),
        ),
        default="STANDARD",
    )
    show_constraint_settings: bpy.props.BoolProperty(
        name="Constraint Settings",
        description="Show or hide constraint settings",
        default=True,
    )
    constraint_input: bpy.props.EnumProperty(
        name="Input",
        description="Type of data used to constrain generation",
        items=(
            ("POSE", "Pose", "Use a pose or animation range from an armature"),
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
    constraint_source_armature: bpy.props.PointerProperty(
        name="Armature",
        description=(
            "Armature whose evaluated pose is converted to NPZ; "
            "empty uses the active armature"
        ),
        type=bpy.types.Object,
        poll=constraint_utils.armature_poll,
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


def _get_strip_for_clip(
    obj: bpy.types.Object, clip
) -> bpy.types.NlaStrip | None:
    if obj.animation_data is None:
        return None

    nla_track = obj.animation_data.nla_tracks.get(clip.name)
    if nla_track is None:
        return None

    return nla_track.strips.get(clip.name)


def _find_clip_at_exact_range(
    obj: bpy.types.Object, start_frame: float, end_frame: float
):
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
    temperature: float,
    model: str,
    in_place: bool,
    duration: float,
):
    new_attempt = clip.attempts.add()
    new_attempt.uid = uuid.uuid4().hex[:6]
    new_attempt.prompt = prompt
    new_attempt.temperature = temperature
    new_attempt.model = model
    new_attempt.in_place = in_place

    n = len(clip.attempts)
    new_attempt.name = f"Take_{n}_{model}_{temperature}"
    new_attempt.action_name = f"{clip.name}_{n}"
    new_attempt.duration = duration

    action = bpy.data.actions.new(name=new_attempt.action_name)
    slot = action.slots.new(obj.id_type, name=obj.name)
    clip.active_attempt = new_attempt.uid
    return new_attempt, action, slot


def _configure_attempt_constraints(
    attempt,
    *,
    use_constraints: bool,
    constraint_input: str = "POSE",
    constraint_type: str = "fullbody",
    joint_name=None,
    source_frame: int = 0,
    target_frame: int = 0,
    num_frames: int = 1,
    text_weight: float = 2.0,
    constraint_weight: float = 2.5,
    first_heading: float = 0.0,
    direction=(0.0, 1.0, 0.0),
    pose_payload: bytes | None = None,
) -> None:
    attempt.use_constraints = bool(use_constraints)
    if not use_constraints:
        return

    attempt.constraint_input = constraint_input
    attempt.constraint_type = constraint_type
    attempt.constraint_joint_name = (
        set(constraint_utils.selected_joint_names(joint_name))
        if constraint_type == "end-effector"
        else set()
    )
    attempt.constraint_source_frame = max(0, int(source_frame))
    attempt.constraint_target_frame = max(0, int(target_frame))
    attempt.constraint_num_frames = max(1, int(num_frames))
    attempt.constraint_text_weight = float(text_weight)
    attempt.constraint_weight = float(constraint_weight)
    attempt.constraint_first_heading = float(first_heading)
    attempt.constraint_direction = direction

    if constraint_input == "POSE":
        if pose_payload is None:
            raise ValueError("Pose constraint payload is missing")
        constraint_utils.cache_constraint_payload(attempt.uid, pose_payload)


def _copy_attempt_constraints(source_attempt, target_attempt) -> None:
    _configure_attempt_constraints(
        target_attempt,
        use_constraints=source_attempt.use_constraints,
        constraint_input=source_attempt.constraint_input,
        constraint_type=source_attempt.constraint_type,
        joint_name=constraint_utils.selected_joint_names(
            source_attempt.constraint_joint_name
        ),
        source_frame=source_attempt.constraint_source_frame,
        target_frame=source_attempt.constraint_target_frame,
        num_frames=source_attempt.constraint_num_frames,
        text_weight=source_attempt.constraint_text_weight,
        constraint_weight=source_attempt.constraint_weight,
        first_heading=source_attempt.constraint_first_heading,
        direction=tuple(source_attempt.constraint_direction),
        pose_payload=(
            constraint_utils.get_constraint_payload(source_attempt.uid)
            if source_attempt.constraint_input == "POSE"
            else None
        ),
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

    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _strip_handoff_frame(nla_strip: bpy.types.NlaStrip) -> float:
    return float(nla_strip.frame_start_ui + nla_strip.blend_in)


def _strip_action_frame_at_timeline(
    nla_strip: bpy.types.NlaStrip, timeline_frame: float
) -> float:
    local_frame = float(nla_strip.action_frame_start + (timeline_frame - nla_strip.frame_start_ui))
    return max(float(nla_strip.action_frame_start), min(float(nla_strip.action_frame_end), local_frame))


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


def _align_strip_root_motion(obj: bpy.types.Object, nla_strip: bpy.types.NlaStrip) -> None:
    previous_strip = _find_preceding_strip(obj, nla_strip)
    if previous_strip is None or previous_strip.action is None or nla_strip.action is None:
        return

    handoff_frame = _strip_handoff_frame(nla_strip)
    target_frame = _strip_action_frame_at_timeline(previous_strip, handoff_frame)
    source_frame = _strip_action_frame_at_timeline(nla_strip, handoff_frame)
    target_location = animation_utils.evaluate_root_location(obj, previous_strip.action, target_frame)
    if target_location is None:
        target_location = animation_utils.Vector((0.0, 0.0, 0.0))

    animation_utils.offset_root_location(
        obj,
        nla_strip.action,
        source_frame=source_frame,
        target_location=target_location,
    )


def _realign_following_strips(
    obj: bpy.types.Object, changed_strip: bpy.types.NlaStrip
) -> None:
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

        self._request = {
            "prompt": str(attempt.prompt),
            "duration": float(attempt.duration),
            "temperature": float(attempt.temperature),
            "model": str(attempt.model),
            "in_place": bool(attempt.in_place),
            "use_constraints": bool(attempt.use_constraints),
        }
        if attempt.use_constraints:
            pose_payload = None
            if attempt.constraint_input == "POSE":
                pose_payload = constraint_utils.get_constraint_payload(attempt.uid)
                if pose_payload is None:
                    self.report(
                        {"ERROR"},
                        "Pose constraint is no longer in memory; create the take again.",
                    )
                    return {"CANCELLED"}
            self._request.update(
                {
                    "constraint_input": str(attempt.constraint_input),
                    "constraint_type": str(attempt.constraint_type),
                    "joint_name": constraint_utils.selected_joint_names(
                        attempt.constraint_joint_name
                    ),
                    "source_frame": int(attempt.constraint_source_frame),
                    "target_frame": int(attempt.constraint_target_frame),
                    "num_frames": int(attempt.constraint_num_frames),
                    "text_weight": float(attempt.constraint_text_weight),
                    "constraint_weight": float(attempt.constraint_weight),
                    "first_heading": float(attempt.constraint_first_heading),
                    "direction": tuple(float(value) for value in attempt.constraint_direction),
                    "constraint_pose": pose_payload,
                }
            )

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
            if request["use_constraints"]:
                is_pose_constraint = request["constraint_input"] == "POSE"
                raw = get_animation_constraints(
                    prompt=request["prompt"],
                    num_frames=request["num_frames"],
                    constraint_type=request["constraint_type"],
                    source_frame=(request["source_frame"] if is_pose_constraint else 0),
                    target_frame=(request["target_frame"] if is_pose_constraint else None),
                    joint_name=request["joint_name"],
                    text_weight=request["text_weight"],
                    constraint_weight=request["constraint_weight"],
                    first_heading=request["first_heading"],
                    direction=(None if is_pose_constraint else list(request["direction"])),
                    constraint_pose=(request["constraint_pose"] if is_pose_constraint else None),
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


class AVACAPO_OT_add_clip(bpy.types.Operator):
    bl_idname = "avacapo.add_clip"
    bl_label = "Add new clip"

    prompt: bpy.props.StringProperty()
    start: bpy.props.IntProperty()
    end: bpy.props.IntProperty()
    transition: bpy.props.IntProperty()
    fadein: bpy.props.IntProperty()
    fadeout: bpy.props.IntProperty()
    temperature: bpy.props.FloatProperty()
    model: bpy.props.StringProperty()
    in_place: bpy.props.BoolProperty()
    generation_mode: bpy.props.StringProperty(default="STANDARD")
    constraint_input: bpy.props.StringProperty(default="POSE")
    constraint_type: bpy.props.StringProperty(default="fullbody")
    constraint_joint_name: bpy.props.EnumProperty(
        items=constraint_utils.END_EFFECTOR_ITEMS,
        options={"ENUM_FLAG"},
        default={"LeftFoot"},
    )
    constraint_source_frame: bpy.props.IntProperty(default=0, min=0)
    constraint_target_frame: bpy.props.IntProperty(default=0, min=0)
    constraint_text_weight: bpy.props.FloatProperty(default=2.0, min=0.0)
    constraint_weight: bpy.props.FloatProperty(default=2.5, min=0.0)
    constraint_first_heading: bpy.props.FloatProperty(default=0.0)
    constraint_direction: bpy.props.FloatVectorProperty(size=3)

    def execute(self, context):
        if State.server_busy:
            self.report({"INFO"}, "Generation already in progress.")
            return {"CANCELLED"}

        obj = context.object
        settings = context.scene.avacapo_settings
        requested_frame_count = _frame_count_from_bounds(self.start, self.end)
        use_constraints = self.generation_mode == "CONSTRAINTS"
        pose_payload = None
        source_frame = int(self.constraint_source_frame)

        if use_constraints:
            validation_error = constraint_utils.validate_constraint_settings(
                context, settings
            )
            if validation_error:
                self.report({"ERROR"}, validation_error)
                return {"CANCELLED"}

            if self.constraint_input == "POSE":
                source = constraint_utils.source_armature(context, settings)
                try:
                    pose_payload, source_frame_count = (
                        constraint_utils.create_pose_constraint_npz(
                            context,
                            source,
                            settings.constraint_pose_source,
                        )
                    )
                except Exception as exc:
                    message = f"Pose constraint conversion failed: {exc}"
                    log.exception(message)
                    self.report({"ERROR"}, message)
                    return {"CANCELLED"}
                if source_frame_count == 1:
                    source_frame = 0

        constraint_options = {
            "use_constraints": use_constraints,
            "constraint_input": self.constraint_input,
            "constraint_type": self.constraint_type,
            "joint_name": constraint_utils.selected_joint_names(
                self.constraint_joint_name
            ),
            "source_frame": source_frame,
            "target_frame": self.constraint_target_frame,
            "num_frames": requested_frame_count,
            "text_weight": self.constraint_text_weight,
            "constraint_weight": self.constraint_weight,
            "first_heading": self.constraint_first_heading,
            "direction": tuple(self.constraint_direction),
            "pose_payload": pose_payload,
        }

        obj.animation_data_create()

        existing_clip, existing_strip = _find_clip_at_exact_range(obj, self.start, self.end)
        if existing_clip is not None and existing_strip is not None:
            existing_clip.prompt = self.prompt
            existing_clip.temperature = self.temperature
            existing_clip.model = self.model
            existing_clip.in_place = self.in_place

            new_attempt, action, slot = _create_attempt_for_clip(
                obj,
                existing_clip,
                prompt=self.prompt,
                temperature=self.temperature,
                model=self.model,
                in_place=self.in_place,
                duration=requested_frame_count / get_fps(),
            )
            _configure_attempt_constraints(new_attempt, **constraint_options)
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
        new_clip.temperature = self.temperature
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
            temperature=self.temperature,
            model=self.model,
            in_place=self.in_place,
            duration=requested_frame_count / get_fps(),
        )
        _configure_attempt_constraints(new_attempt, **constraint_options)

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
            (
                attempt
                for attempt in clip.attempts
                if attempt.uid == clip.active_attempt
            ),
            None,
        )
        if source_attempt is None:
            self.report({"ERROR"}, "Active take not found.")
            return {"CANCELLED"}
        if (
            source_attempt.use_constraints
            and source_attempt.constraint_input == "POSE"
            and constraint_utils.get_constraint_payload(source_attempt.uid) is None
        ):
            self.report(
                {"ERROR"},
                "Pose constraint is no longer in memory; create a new constrained clip.",
            )
            return {"CANCELLED"}

        new_attempt, action, slot = _create_attempt_for_clip(
            obj,
            clip,
            prompt=clip.prompt,
            temperature=clip.temperature,
            model=clip.model,
            in_place=clip.in_place,
            duration=_frame_count_from_bounds(
                nla_strip.frame_start_ui, nla_strip.frame_end_ui
            )
            / get_fps(),
        )
        _copy_attempt_constraints(source_attempt, new_attempt)

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

_classes = [
    AvacapoSettings,
    AvacapoAttempt,
    AvacapoClip,
    AvacapoClips,
    AVACAPO_OT_login_browser,
    AVACAPO_OT_paste_token,
    AVACAPO_OT_disconnect,
    AVACAPO_OT_reload_addon,
    AVACAPO_OT_add_clip,
    AVACAPO_OT_new_attempt,
    AVACAPO_OT_fetch,
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
