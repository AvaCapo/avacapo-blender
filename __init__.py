from typing import Self
import bpy
import os
import threading
import time
import webbrowser


from .state_controller import State, Queue
from .server import get_animation, get_fps
from . import animation_utils
from . import rig_utils
from .storage import Storage
from .logger import log
from .config import Config
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
    def update_start(self, context):
        self.end = self.start + self.end

    def update_duration(self, context):
        self.end = int(self.start + (self.duration * get_fps()))

    def update_end(self, context):
        self.duration = (self.end - self.start) / get_fps()

    text_block: bpy.props.PointerProperty(type=bpy.types.Text)
    prompt: bpy.props.StringProperty(
        name="", default="A person is walking backward", description="Enter text here"
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
        default=120,
        update=update_end,
    )
    temperature: bpy.props.FloatProperty(
        name="temperature",
        description="setting 0.0 - 1.0 for neural network 'randomness'",
        default=config.DEFAULT_TEMPERATURE,
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=[(m, f"{m.capitalize()}", f"{m} model") for m in config.DEFAULT_MODELS],
        default=config.DEFAULT_MODELS[0],
    )
    token_input: bpy.props.StringProperty(
        name="Token",
        description="Paste your API token here",
        default="",
        subtype="PASSWORD",
    )


# stored locally (on Object), bound to single nla track
# and have several "attempts"(requests/actions)
class AvacapoTask(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(
        name="", default="A person is walking backward", description="Enter text here"
    )
    start: bpy.props.IntProperty(
        name="start",
        default=0,
    )
    duration: bpy.props.FloatProperty(
        name="duration",
        description="seconds",
        default=2.5,
    )
    end: bpy.props.IntProperty(
        name="end",
        default=120,
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
    attempts: ...
    nla_track: ...


# attempt - a single trial, or
class AvacapoAttempt(bpy.types.PropertyGroup):
    temperature: bpy.props.FloatProperty(
        name="temperature",
        description="setting 0.0 - 1.0 for neural network 'randomness'",
        default=config.DEFAULT_TEMPERATURE,
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=[(m, f"{m.capitalize()}", f"{m} model") for m in config.DEFAULT_MODELS],
        default=config.DEFAULT_MODELS[0],
    )
    token_input: bpy.props.StringProperty(
        name="Token",
        description="Paste your API token here",
        default="",
        subtype="PASSWORD",
    )
    queue_task: ...
    # when done:
    action: ...


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

    def modal(self, context, event):
        time_start = time.time()
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._thread and not self._thread.is_alive():
            log.debug("Background thread finished, cleaning up timer")
            context.window_manager.event_timer_remove(self._timer)

            task_id = State.current_task_id

            State.server_busy = False
            State.current_task_id = ""
            State.server_status = ""

            if self._error:
                msg = f"Request failed: {self._error}"
                log.error(msg)
                self.report({"ERROR"}, msg)
                State.server_status = f"Error: {self._error}"
                if task_id:
                    Queue.finish(
                        task_id,
                        status="error",
                        generation_time=time.time() - self._time_start,
                    )
                return {"CANCELLED"}

            log.info(f"BVH received: {self._result!r}")
            try:
                self._execute(self._result, context.object)
            except Exception as exc:
                message = f"Applying animation failed: {exc}"
                log.exception(message)
                self.report({"ERROR"}, message)
                State.server_status = f"Error: {exc}"
                if task_id:
                    Queue.finish(
                        task_id,
                        status="error",
                        generation_time=time.time() - self._time_start,
                    )
                return {"CANCELLED"}

            if task_id:
                Queue.finish(
                    task_id,
                    status="done",
                    generation_time=time.time() - self._time_start,
                )
            State.server_status = "Done!"
            self.report({"INFO"}, "animation applied")
            time_end = time.time()
            log.debug(f"time of modal operator animation apply is {time_end - time_start}")

            # chain: process the next pending task if any
            bpy.ops.queue.process()
            return {"FINISHED"}

        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        self._time_start = time.time()
        if State.server_busy:
            self.report({"WARNING"}, "Already fetching, please wait...")
            return {"CANCELLED"}

        log.debug("Starting fetch")
        State.server_busy = True
        State.server_status = "Fetching..."
        self._result = None
        self._error = None

        self._thread = threading.Thread(target=self._fetch, daemon=True)
        self._thread.start()

        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _fetch(self):
        settings = bpy.context.scene.avacapo_settings
        log.debug("Thread started, opening URL...")
        try:
            raw = get_animation(
                prompt=settings.prompt,
                duration=settings.duration,
                temperature=settings.temperature,
                model=settings.model,
            )
            log.debug(f"Raw response ({len(raw)} bytes): {raw[:120]}")
            self._result = raw
        except Exception as e:
            self._error = str(e)
            log.exception("Unexpected error in fetch thread")

    @staticmethod
    def _execute(fetch_result, obj):
        log.debug("applying animation")
        animation_utils.apply_bvh(obj, fetch_result)


class QUEUE_OT_redo_task(bpy.types.Operator):
    bl_idname = "queue.redo_task"
    bl_label = "Redo Task"

    task_id: bpy.props.StringProperty()

    def execute(self, context):
        original = Queue.get_by_id(self.task_id)
        if not original:
            self.report({"WARNING"}, "Task not found.")
            return {"CANCELLED"}

        # clone with same params, fresh status
        try:
            task = Queue.clone_task(original)
        except ValueError as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}

        bpy.ops.queue.discard_task(task_id=self.task_id)
        self.report({"INFO"}, f"Re-queued: {task.id}")
        if not State.server_busy:
            bpy.ops.queue.process()
        return {"FINISHED"}


class QUEUE_OT_discard_task(bpy.types.Operator):
    bl_idname = "queue.discard_task"
    bl_label = "Discard Task"

    task_id: bpy.props.StringProperty()

    def execute(self, context):
        Queue.discard_by_id(self.task_id)
        return {"FINISHED"}


class QUEUE_OT_add_task(bpy.types.Operator):
    bl_idname = "queue.add_task"
    bl_label = "Add Task"
    bl_description = "Queue a generation task from the current prompt"

    def execute(self, context):
        settings = context.scene.avacapo_settings
        prompt = settings.prompt.strip()

        if not prompt:
            self.report({"WARNING"}, "Prompt is empty.")
            return {"CANCELLED"}

        try:
            task = Queue.add(
                prompt=settings.prompt.strip(),
                start_frame=settings.start,
                duration=settings.duration,
                model=settings.model,
            )
        except ValueError as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}

        self.report({"INFO"}, f"Task queued: {task.name} [{task.id}]")

        # kick off processing immediately if nothing is running
        if not State.server_busy:
            bpy.ops.queue.process()

        return {"FINISHED"}


class QUEUE_OT_process(bpy.types.Operator):
    """Pick the next pending task and start the fetch operator"""

    bl_idname = "queue.process"
    bl_label = "Process Queue"

    def execute(self, context):
        if State.server_busy:
            return {"FINISHED"}  # fetch already running, it will chain on its own

        task = Queue.start_next()
        if task is None:
            return {"FINISHED"}

        State.current_task_id = task.id
        bpy.ops.avacapo.fetch("INVOKE_DEFAULT")
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
    QUEUE_OT_add_task,
    QUEUE_OT_process,
    QUEUE_OT_redo_task,
    QUEUE_OT_discard_task,
    AVACAPO_OT_login_browser,
    AVACAPO_OT_paste_token,
    AVACAPO_OT_disconnect,
    AVACAPO_OT_reload_addon,
    AVACAPO_OT_fetch,
    AVACAPO_OT_create_avacapo,
    AVACAPO_OT_create_avacapo_v1,
    AVACAPO_OT_select_by_name,
    AVACAPO_PT_main_panel,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.avacapo_settings = bpy.props.PointerProperty(type=AvacapoSettings)
    bpy.app.handlers.depsgraph_update_post.append(rig_utils._init_bone_trees_once)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.avacapo_settings
    if rig_utils._init_bone_trees_once in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(rig_utils._init_bone_trees_once)
