import bpy
import os
import threading
import time
import webbrowser
import uuid

from bpy.props import CollectionProperty


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


# Props:
# --------------------------------------------------------------------


# remember - we send to server (duration and fps)
# stored globally (on Scene), to create a new "task" and start "attempt"(request) on it
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
        default=2.5,
        update=update_duration,
    )
    end: bpy.props.IntProperty(
        name="end",
        default=int(config.DEFAULT_DURATION * get_fps()),
        update=update_end,
    )
    fadein: bpy.props.IntProperty(
        name="fadein",
        description="frames",
        default=50,
    )
    fadeout: bpy.props.IntProperty(
        name="fadeout",
        description="frames",
        default=50,
    )
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
    # Take_2_asm_0.8
    # Take_3_gen2_1.0
    uid: bpy.props.StringProperty()
    name: bpy.props.StringProperty()
    action_name: bpy.props.StringProperty()
    status: bpy.props.StringProperty()  # | Error | Pending | Fetching | Done

    # to restart attempt if something goes off
    # all the request props to restart the attempt
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
    start: bpy.props.IntProperty(
        name="start",
        default=0,
    )
    end: bpy.props.IntProperty(
        name="end",
        default=120,
    )
    # duration is calculated live, no note
    fadein: bpy.props.IntProperty(
        name="fadein",
        description="frames",
        default=50,
    )
    fadeout: bpy.props.IntProperty(
        name="fadeout",
        description="frames",
        default=50,
    )
    # here we have request props again,
    # to create new attempts
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

    attempts: CollectionProperty(type=AvacapoAttempt)


class AvacapoClips(bpy.types.PropertyGroup):
    clips: CollectionProperty(type=AvacapoClip)


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
                self._execute(self._result, self.obj_name, self.attempt_uid)
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
            )
            log.debug(f"Raw response ({len(raw)} bytes): {raw[:120]}")
            self._result = raw
        except Exception as e:
            self._error = str(e)
            log.exception("Unexpected error in fetch thread")

    @staticmethod
    def _execute(fetch_result, obj_name, attempt_uid):
        obj = bpy.data.objects[obj_name]
        attempt = animation_utils.find_attempt(obj, attempt_uid)
        action = bpy.data.actions[attempt.action_name]
        if not action:
            log.debug("no action found")
        else:
            animation_utils.apply_animation(obj, fetch_result, action)


class Avacapo_OT_add_clip(bpy.types.Operator):
    bl_idname = "avacapo.add_clip"
    bl_label = "Add new clip"

    prompt: bpy.props.StringProperty()
    start: bpy.props.IntProperty()
    end: bpy.props.IntProperty()
    fadein: bpy.props.IntProperty()
    fadeout: bpy.props.IntProperty()
    temperature: bpy.props.FloatProperty()
    model: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.object

        # create the clip props
        new_clip = obj.avacapo_clips.clips.add()
        new_clip.prompt = self.prompt
        new_clip.name = new_clip.create_name()
        new_clip.start = self.start
        new_clip.end = self.start
        new_clip.fadein = self.fadein
        new_clip.fadeout = self.fadeout

        # generate new attempt
        new_attempt = new_clip.attempts.add()
        new_attempt.uid = uuid.uuid4().hex[:6]
        new_attempt.prompt = self.prompt
        # request params
        new_attempt.temperature = self.temperature
        new_attempt.model = self.model
        n = len(new_clip.attempts)
        new_attempt.name = f"Take_{n}_{self.model}_{self.temperature}"
        new_attempt.action_name = f"{new_clip.name}_{n}"
        new_attempt.duration = (self.end - self.start) / get_fps()
        bpy.data.actions.new(name=new_attempt.action_name)

        # add the attempt to queue and start fetching it
        # GlobalQueue.add_attempt(new_attempt.uid)

        # because you cannot pass custom datablock into operator
        bpy.ops.avacapo.fetch(
            "INVOKE_DEFAULT",
            clip_name=new_clip.name,
            attempt_uid=new_attempt.uid,
            obj_name=obj.name,
        )

        # bind nla
        nla_tracks = obj.animation_data.nla_tracks
        nla_track = nla_tracks.new(prev=None)
        nla_track.name = new_clip.name
        action = bpy.data.actions.get(new_attempt.action_name)
        nla_strip = nla_track.strips.new(
            name=new_attempt.action_name,
            start=self.start,
            action=action,
        )
        nla_strip.action_frame_start = 1
        nla_strip.action_frame_end = self.end
        nla_strip.blend_in = self.fadein
        nla_strip.blend_out = self.fadeout
        nla_strip.blend_type = "REPLACE"

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
    """create avacapo rig"""

    bl_idname = "avacapo.create_avacapo_v1"
    bl_label = "Create avacapo rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        inner_path = "Object"
        object_name = config.AVACAPO_RIG_NAME

        bpy.ops.wm.append(
            filepath=os.path.join(config.BLEND_PATH, inner_path, object_name),
            directory=os.path.join(config.BLEND_PATH, inner_path),
            filename=object_name,
        )

        obj = context.scene.objects.get(object_name)
        if obj:
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            context.view_layer.objects.active = obj
            self.report({"INFO"}, "avacapo rig created!")
        else:
            self.report({"WARNING"}, f"Object '{object_name}' not found after append")
            return {"CANCELLED"}

        return {"FINISHED"}


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
            {"INFO"}, f"start record lock in {"on" if settings.start_record_lock else "off"}!"
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
    Avacapo_OT_add_clip,
    AVACAPO_OT_fetch,
    AVACAPO_OT_create_avacapo,
    AVACAPO_OT_create_avacapo_v1,
    AVACAPO_OT_select_by_name,
    AVACAPO_OT_toggle_start_record_lock,
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
