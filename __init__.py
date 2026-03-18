import bpy
import os
import threading
import time
import datetime


from .state_controller import State, Queue
from .server import get_animation
from . import animation_utils
from . import rig_utils
from .storage import Storage
from .logger import log
from .config import AVACAPO_RIG_NAME, BLEND_PATH

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


class AvacapoSettings(bpy.types.PropertyGroup):
    text_block: bpy.props.PointerProperty(type=bpy.types.Text)
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
    temperature: bpy.props.FloatProperty(
        name="temperature",
        description="setting 0.0 - 1.0 for neural network 'randomness'",
        default=1.0,
    )
    # TODO: get from config
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=[
            ("asm", "Asm", "Fast simple ASM"),
            ("gen1", "Gen 1", "First generation GPAT"),
            ("gen2", "Gen 2", "Second generation GPAT"),
        ],
        default="asm",
    )


# Operators:
# --------------------------------------------------------------------


class AVACAPO_OT_fetch(bpy.types.Operator):
    bl_idname = "aitext.fetch"
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

            # update the task that was running
            task = Queue.get_by_id(State.current_task_id)

            State.server_busy = False
            State.current_task_id = ""
            State.server_status = ""

            if self._error:
                msg = f"Request failed: {self._error}"
                log.error(msg)
                self.report({"ERROR"}, msg)
                State.server_status = f"Error: {self._error}"
                if task:
                    task.status = "error"
                return {"CANCELLED"}

            log.info(f"BVH received: {self._result!r}")
            if task:
                task.status = "done"
            self._execute(self._result, context.object)
            State.server_status = "Done!"
            self.report({"INFO"}, "animation applied")
            time_end = time.time()
            log.debug(
                f"time of modal operator animation apply is {time_end - time_start}"
            )

            # chain: process the next pending task if any
            bpy.ops.queue.process()

            task = Queue.get_by_id(State.current_task_id)
            if task:
                task.status = "error" if self._error else "done"
                task.time_finished = datetime.datetime.now().isoformat(
                    timespec="seconds"
                )
                task.generation_time = time.time() - self._time_start
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

        self._timer = context.window_manager.event_timer_add(
            0.25, window=context.window
        )
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
        bpy.ops.queue.discard_task(task_id=self.task_id)
        task = Queue.add(
            prompt=original.prompt,
            start_frame=original.start_frame,
            duration=original.duration,
            model=original.model,
        )
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

        task = Queue.add(
            prompt=settings.prompt.strip(),
            start_frame=settings.start,
            duration=settings.duration,
            model=settings.model,
        )

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

        task = next((t for t in Queue.tasks if t.status == "pending"), None)
        if task is None:
            return {"FINISHED"}

        task.status = "loading"
        State.current_task_id = task.id
        bpy.ops.aitext.fetch("INVOKE_DEFAULT")
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
        object_name = AVACAPO_RIG_NAME

        bpy.ops.wm.append(
            filepath=os.path.join(BLEND_PATH, inner_path, object_name),
            directory=os.path.join(BLEND_PATH, inner_path),
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

    def invoke(self, context, event: bpy.types.Event | None) -> set[str]:
        return self.execute(context)


class AVACAPO_OT_update_addon(bpy.types.Operator):
    """update current addon after update"""

    bl_idname = "avacapo.update_addon"
    bl_label = "update Addon"
    bl_options = {"REGISTER"}

    def execute(self, context):
        self.report({"INFO"}, "addon updated!")
        return {"FINISHED"}

    def invoke(self, context, event: bpy.types.Event | None) -> set[str]:
        return self.execute(context)


class AVACAPO_OT_create_avacapo(bpy.types.Operator):
    """Not yet implemented"""

    bl_idname = "avacapo.create_avacapo"
    bl_label = "Create avacapo"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        self.report({"INFO"}, "avacapo created!")
        return {"FINISHED"}

    def invoke(self, context, event: bpy.types.Event | None) -> set[str]:
        return self.execute(context)


class MY_OT_OpenTextPopover(bpy.types.Operator):
    bl_idname = "my.open_text_popover"
    bl_label = "Preview Text"

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=400)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.avacapo_settings
        text = settings.prompt

        row = layout.row()
        row.label(text="Preview:", icon="TEXT")
        row.label(text=context.object.name, icon="OUTLINER_OB_ARMATURE")
        row.label(text="5s", icon="TIME")
        box = layout.box()

        if text:
            for line in text.split("\n"):
                box.label(text=line if line else " ")
        else:
            box.label(text="(empty)", icon="INFO")

        if not Queue.allow_new_task:
            layout.label(text="Too musch in the queue, wait...", icon="TIME")
        else:
            layout.operator(
                QUEUE_OT_add_task.bl_idname,
                text="Generate",
                icon="SHADERFX",
            )
        if State.server_status:
            icon = "ERROR" if "Error" in State.server_status else "INFO"
            layout.label(text=State.server_status, icon=icon)
        layout.prop(settings, "model")

    def execute(self, context):
        return {"FINISHED"}


# UTILS
# --------------------------------------------------------------------


def check_type_of_armature(armature):
    return "AVACAPO_V1"


def get_selected_obj(context) -> bpy.types.Object | None:
    return context.object


# Panel
# --------------------------------------------------------------------


class AVACAPO_PT_main_panel(bpy.types.Panel):
    """Main avacapo panel in the N panel"""

    bl_label = "Avacapo"
    bl_idname = "AVACAPO_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Avacapo"

    def draw(self, context) -> None:
        layout = self.layout
        row = layout.row(align=True)

        if Storage.api_token != "":
            row.label(text="Connected", icon="INTERNET")
        else:
            row.label(text="Disconnected", icon="ERROR")
        row.operator(
            AVACAPO_OT_reload_addon.bl_idname,
            text="",
            icon="MESH_UVSPHERE",
        )
        box_obj = layout.box()
        settings = context.scene.avacapo_settings

        selected_obj = get_selected_obj(context)
        if selected_obj is None or selected_obj.type != "ARMATURE":
            if selected_obj is None:
                box_obj.label(text="no selected object", icon="ERROR")
            elif selected_obj.type != "ARMATURE":
                box_obj.label(
                    text=f"{context.object.name} is not an armature",
                    icon="MOD_WIREFRAME",
                )
            # TODO: remove double pass trough objects
            if any(o.type == "ARMATURE" for o in context.scene.objects):
                for a in [o for o in context.scene.objects if o.type == "ARMATURE"]:
                    row_select_armature = box_obj.row()
                    op = row_select_armature.operator(
                        AVACAPO_OT_select_by_name.bl_idname,
                        text="",
                        icon="RESTRICT_SELECT_OFF",
                    )
                    op.obj_name = a.name
                    row_select_armature.label(text=a.name, icon="OUTLINER_OB_ARMATURE")
            else:
                box_obj.label(text="no armatures", icon="OUTLINER_OB_ARMATURE")
            box_obj.operator(
                AVACAPO_OT_create_avacapo_v1.bl_idname,
                text="New Armature",
                icon="OUTLINER_OB_ARMATURE",
            )
        else:
            armature = context.object
            box_obj.label(text=f"{context.object.name}", icon="OUTLINER_OB_ARMATURE")
            if rig_utils.infer_rig_type(armature) != "avacapo_bvh_v1":
                box_obj.label(text="Unknown rig", icon="ERROR")
                box_obj.operator(
                    AVACAPO_OT_create_avacapo.bl_idname,
                    text="Try to Convert",
                    icon="SHADERFX",
                )
            else:
                box_obj.label(text="AvaCapo rig v1", icon="CHECKBOX_HLT")
                box_prompt = layout.box()
                row_top = box_prompt.row(align=True)
                col = row_top.row(align=True)
                col.label(text="Start")
                row_top.separator()
                row_top.separator()
                row_top.label(text="Duration", icon="TIME")
                row_top.separator()
                row_top.separator()
                row_top.label(text="End")
                row = box_prompt.row(align=True)
                row.operator(
                    AVACAPO_OT_create_avacapo.bl_idname, text="", icon="RECORD_ON"
                )
                row.prop(settings, "start", text="", expand=True)
                row.separator()
                row.separator()
                row.prop(settings, "duration", text="", expand=True)
                row.separator()
                row.separator()
                row.prop(settings, "end", text="", expand=True)
                row.operator(
                    AVACAPO_OT_create_avacapo.bl_idname, text="", icon="RECORD_ON"
                )
                box_prompt.separator(type="LINE")
                row_desc = box_prompt.row(align=True)
                row_desc.label(text="Prompt:", icon="TEXT")
                row_desc.operator(
                    "my.open_text_popover", text="", icon="FULLSCREEN_ENTER"
                )
                row_prompt = box_prompt.row(align=True)
                row_prompt.prop(settings, "prompt")
                row = box_prompt.row(align=True)
                row.prop(settings, "model")

                # Generate button — now queues a task instead of fetching directly
                if not Queue.allow_new_task:
                    row.label(text="Queue is full...", icon="TIME")
                else:
                    row.operator(
                        QUEUE_OT_add_task.bl_idname,
                        text="Generate",
                        icon="SHADERFX",
                    )

                if "Error" in State.server_status:
                    layout.label(text=State.server_status, icon=icon)

                # Queue
                Queue.draw(layout)


# Registration
# --------------------------------------------------------------------

_classes = [
    AvacapoSettings,
    MY_OT_OpenTextPopover,
    QUEUE_OT_add_task,
    QUEUE_OT_process,
    QUEUE_OT_redo_task,
    QUEUE_OT_discard_task,
    AVACAPO_OT_reload_addon,
    AVACAPO_OT_update_addon,
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
