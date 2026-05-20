import bpy
import os
import threading
import time
import webbrowser
import uuid
import textwrap


from .state_controller import frame_change_post
from .server import get_animation, get_fps
from . import animation_utils
from . import rig_utils
from .storage import Storage
from .logger import log
from .config import Config
from .models import clear_models_cache, get_model_enum_items, resolve_model_type
from .ui import AVACAPO_PT_main_panel

config = Config()
bl_info = {
    "name": "AvaCapo AI animation",
    "author": "agamurian",
    "version": (0, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Avacapo",
    "description": "Automating charecter animation with AI",
    "category": "Animation",
}



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

    def update_duration(self, context):
        self._set_time_fields(
            lambda: setattr(self, "end", int(round(self.start + (self.duration * self._fps()))))
        )

    def update_end(self, context):
        self._set_time_fields(
            lambda: setattr(self, "duration", (self.end - self.start) / self._fps())
        )

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
    token_input: bpy.props.StringProperty(
        name="Token",
        description="Paste your API token here",
        default="",
        subtype="PASSWORD",
    )


# stored locally (on Object), bound to single nla track
# and have several "attempts"(requests/actions)
# attempt - a single trial, or
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


def _chain_start_frame(
    obj: bpy.types.Object,
    requested_start: int,
    requested_frame_count: int,
    transition_frames: int,
) -> tuple[int, int]:
    latest_strip = _find_latest_strip(obj)
    if latest_strip is None:
        return requested_start, 0

    safe_transition = min(
        max(0, int(transition_frames)),
        max(0, requested_frame_count - 1),
        max(0, _frame_count_from_strip(latest_strip) - 1),
    )
    start_frame = int(round(latest_strip.frame_end_ui - safe_transition))
    return start_frame, safe_transition


def _move_generation_cursor(settings: AvacapoSettings, frame_end: float) -> None:
    settings.start = int(round(frame_end))


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


def _align_strip_root_motion(obj: bpy.types.Object, nla_strip: bpy.types.NlaStrip) -> None:
    previous_strip = _find_preceding_strip(obj, nla_strip)
    if previous_strip is None or previous_strip.action is None or nla_strip.action is None:
        return

    handoff_frame = _strip_handoff_frame(nla_strip)
    target_frame = _strip_action_frame_at_timeline(previous_strip, handoff_frame)
    source_frame = _strip_action_frame_at_timeline(nla_strip, handoff_frame)
    target_location = animation_utils.evaluate_root_location(obj, previous_strip.action, target_frame)
    if target_location is None:
        return

    animation_utils.offset_root_location(
        obj,
        nla_strip.action,
        source_frame=source_frame,
        target_location=target_location,
    )


# Auth Operators:
# --------------------------------------------------------------------


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
        url = f"{config.FRONTEND_URL}/app/api-tokens?plugin_auth=true&port={port}"
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
        self.report({"INFO"}, "Disconnected")
        return {"FINISHED"}


# Operators:
# --------------------------------------------------------------------


class AVACAPO_OT_fetch(bpy.types.Operator):
    bl_idname = "avacapo.fetch"
    bl_label = "Get Animation"
    bl_description = "Get AI Generated animation"

    _thread = None
    _result = None
    _error = None
    _timer = None

    clip_name: bpy.props.StringProperty()
    attempt_uid: bpy.props.StringProperty()
    obj_name: bpy.props.StringProperty()

    def modal(self, context, event):
        time_start = time.time()
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._thread and not self._thread.is_alive():
            log.debug("Background thread finished, cleaning up timer")
            context.window_manager.event_timer_remove(self._timer)

            if self._error:
                msg = f"Request failed: {self._error}"
                log.error(msg)
                self.report({"ERROR"}, msg)
                return {"CANCELLED"}

            log.info(f"BVH received: {self._result!r}")
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

        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        self._time_start = time.time()
        self._result = None
        self._error = None

        self._thread = threading.Thread(target=self._fetch, daemon=True)
        self._thread.start()

        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _fetch(self):
        attempt = animation_utils.find_attempt(self.obj_name, self.attempt_uid)
        selected_model = resolve_model_type(attempt.model)
        log.debug("Thread started, opening URL...")
        try:
            raw = get_animation(
                prompt=attempt.prompt,
                duration=attempt.duration,
                temperature=attempt.temperature,
                model=selected_model,
                in_place=attempt.in_place,
            )
            log.debug(f"Raw response ({len(raw)} bytes): {raw[:120]}")
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
            animation_utils.apply_animation(obj, fetch_result, action)
            nla_strip = obj.animation_data.nla_tracks[clip_name].strips[clip_name]
            _align_strip_root_motion(obj, nla_strip)
            _sync_strip_to_action(nla_strip, action)
            _move_generation_cursor(settings, nla_strip.frame_end_ui)
            obj.animation_data.action = None


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

    def execute(self, context):
        obj = context.object
        settings = context.scene.avacapo_settings
        obj.animation_data_create()

        # create the clip props
        new_clip = obj.avacapo_clips.clips.add()
        new_clip.prompt = self.prompt
        new_clip.name = new_clip.create_name()
        new_clip.temperature = self.temperature
        new_clip.model = self.model
        new_clip.in_place = self.in_place

        # generate new attempt
        requested_frame_count = _frame_count_from_bounds(self.start, self.end)
        clip_start, clip_transition = _chain_start_frame(
            obj,
            requested_start=self.start,
            requested_frame_count=requested_frame_count,
            transition_frames=self.transition,
        )

        new_attempt = new_clip.attempts.add()
        new_attempt.uid = uuid.uuid4().hex[:6]
        new_attempt.prompt = self.prompt
        # request params
        new_attempt.temperature = self.temperature
        new_attempt.model = self.model
        new_attempt.in_place = self.in_place
        n = len(new_clip.attempts)
        new_attempt.name = f"Take_{n}_{self.model}_{self.temperature}"
        new_attempt.action_name = f"{new_clip.name}_{n}"
        new_attempt.duration = requested_frame_count / get_fps()
        action = bpy.data.actions.new(name=new_attempt.action_name)
        slot = action.slots.new(obj.id_type, name=obj.name)

        # add the attempt to queue and start fetching it
        # GlobalQueue.add_attempt(new_attempt.uid)

        # bind nla
        nla_tracks = obj.animation_data.nla_tracks
        nla_track = nla_tracks.new(prev=None)
        nla_track.name = new_clip.name
        action = bpy.data.actions.get(new_attempt.action_name)
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
        obj = context.object
        for given_clip in context.object.avacapo_clips.clips:
            if given_clip.name == self.clip_uid:
                clip = given_clip

        nla_strip = obj.animation_data.nla_tracks[clip.name].strips[clip.name]

        new_attempt = clip.attempts.add()
        new_attempt.uid = uuid.uuid4().hex[:6]
        new_attempt.prompt = clip.prompt
        # request params
        new_attempt.temperature = clip.temperature
        new_attempt.model = clip.model
        new_attempt.in_place = clip.in_place
        n = len(clip.attempts)
        new_attempt.name = f"Take_{n}_{clip.model}_{clip.temperature}"
        new_attempt.action_name = f"{clip.name}_{n}"
        new_attempt.duration = (
            _frame_count_from_bounds(nla_strip.frame_start_ui, nla_strip.frame_end_ui)
            / get_fps()
        )
        action = bpy.data.actions.new(name=new_attempt.action_name)
        slot = action.slots.new(obj.id_type, name=obj.name)

        nla_strip.action = action
        nla_strip.action_slot = action.slots[f"OB{obj.name}"]

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

    attempt_uid: bpy.props.StringProperty()
    clip_uid: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.object
        attempt = animation_utils.find_attempt(obj.name, self.attempt_uid)
        for given_clip in context.object.avacapo_clips.clips:
            if given_clip.name == self.clip_uid:
                clip = given_clip

        clip.active_attempt = self.attempt_uid

        nla_strip = obj.animation_data.nla_tracks[clip.name].strips[clip.name]
        action = bpy.data.actions[attempt.action_name]
        nla_strip.action = action
        _sync_strip_to_action(nla_strip, action)

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

        bpy.ops.wm.append(
            filepath=os.path.join(config.BLEND_PATH, inner_path, object_name),
            directory=os.path.join(config.BLEND_PATH, inner_path),
            filename=object_name,
        )

        obj = context.scene.objects.get(object_name)
        if obj:
            return self._select_created_object(context, obj, "AvaCapo rig created!")

        self.report({"WARNING"}, f"Object '{object_name}' not found after append")
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
    AVACAPO_OT_create_avacapo,
    AVACAPO_OT_create_avacapo_v1,
    AVACAPO_OT_select_by_name,
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


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Object.avacapo_clips
    del bpy.types.Scene.avacapo_settings
    bpy.app.handlers.frame_change_post.remove(frame_change_post)
    if rig_utils._init_bone_trees_once in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(rig_utils._init_bone_trees_once)
