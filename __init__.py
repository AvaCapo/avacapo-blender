import bpy
import os
import threading
import time


from .state_controller import State, Queue
from .server import get_animation
from . import animation_utils
from . import rig_utils
from .storage import Storage
from .logger import log
from .config import ADDON_NAME, AVACAPO_RIG_NAME, BLEND_PATH

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


# all blender-local variables
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
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        # TODO: take from config.py, generate enum
        items=[
            ("asm", "Asm", "Fast simple ASM"),
            ("gen1", "Gen 1", "First generation GPAT"),
            ("gen2", "Gen 2", "Second generation GPAT"),
        ],
        default="asm",
    )


# Operators:
# --------------------------------------------------------------------

# Actual server fetch


# Working Example of asyncronous operator
class AVACAPO_OT_fetch(bpy.types.Operator):
    bl_idname = "aitext.fetch"
    bl_label = "Get Animation"
    bl_description = "Get AI Generated animation"

    _thread = None
    _result = None
    _error = None
    _timer = None

    # called every 0.25 s by the timer on the main thread
    def modal(self, context, event):
        time_start = time.time()
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._thread and not self._thread.is_alive():
            log.debug("Background thread finished, cleaning up timer")
            context.window_manager.event_timer_remove(self._timer)
            State.server_busy = False
            State.server_status = ""

            if self._error:
                msg = f"Request failed: {self._error}"
                log.error(msg)
                self.report({"ERROR"}, msg)
                State.server_status = f"Error: {self._error}"
                return {"CANCELLED"}

            log.info(f"BVH received: {self._result!r}")
            self._execute(self._result, context.object)
            State.server_status = "Done!"
            self.report({"INFO"}, "animation applied")
            time_end = time.time()
            log.debug(
                f"time of modal operator animation apply is {time_end - time_start}"
            )
            return {"FINISHED"}

        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        if State.server_busy:
            self.report({"WARNING"}, "Already fetching, please wait...")
            return {"CANCELLED"}

        log.debug(f"Starting fetch from")
        State.server_busy = True
        State.server_status = "Fetching..."
        self._result = None
        self._error = None

        # spin up background thread — urllib blocks, can't run on main thread
        self._thread = threading.Thread(target=self._fetch, daemon=True)
        self._thread.start()

        # modal + timer keeps the operator alive without freezing Blender
        self._timer = context.window_manager.event_timer_add(
            0.25, window=context.window
        )
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    # ── background thread ─────────────────────────────────────────────────────

    def _fetch(self):
        settings = bpy.context.scene.avacapo_settings
        # check response here,
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

    # ── main thread ───────────────────────────────────────────────────────────

    @staticmethod
    def _execute(fetch_result, obj):
        # passing object! stale data, change to custom ids
        log.debug("appling animation")
        animation_utils.apply_bvh(obj, fetch_result)


# Working Example of asyncronous operator


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

        # Optional: select and make it active
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
    bl_options = {"REGISTER"}  # obviously no undo avaliable

    def execute(self, context):
        addon_name = ADDON_NAME
        bpy.ops.script.reload()
        # bpy.ops.wm.addon_enable(module="addon_name")
        self.report({"INFO"}, "addon reloaded!")
        return {"FINISHED"}

    def invoke(self, context, event: bpy.types.Event | None) -> set[str]:
        return self.execute(context)


class AVACAPO_OT_update_addon(bpy.types.Operator):
    """update current addon after update"""

    bl_idname = "avacapo.update_addon"
    bl_label = "update Addon"
    bl_options = {"REGISTER"}  # obviously no undo avaliable

    def execute(self, context):
        addon_name = ADDON_NAME

        self.report({"INFO"}, "addon updateed!")
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
            # Split by newlines and display each line
            for line in text.split("\n"):
                box.label(text=line if line else " ")
        else:
            box.label(text="(empty)", icon="INFO")
        if State.server_busy:
            layout.label(text="Fetching...", icon="TIME")
        else:
            layout.operator(
                AVACAPO_OT_fetch.bl_idname,
                text="Generate",
                icon="SHADERFX",
            )
        if State.server_status:
            icon = "ERROR" if "Error" in State.server_status else "INFO"
            layout.label(text=State.server_status, icon=icon)
        # layout.prop(settings, "temperature")
        layout.prop(settings, "model")

    def execute(self, context):
        return {"FINISHED"}


class QUEUE_OT_add_task(bpy.types.Operator):
    bl_idname = "queue.add_task"
    bl_label = "Add Task"
    bl_description = "Add a new task to the queue"

    prompt: bpy.props.StringProperty(
        name="Prompt",
        description="Task prompt",
        default="",
    )

    def execute(self, context):
        if not self.prompt.strip():
            self.report({"WARNING"}, "Prompt is empty.")
            return {"CANCELLED"}

        task = Queue.add(self.prompt.strip())
        self.report({"INFO"}, f"Task added: {task.name} [{task.id}]")
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "prompt")


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
        # for objects every redraw!!!
        if selected_obj == None or selected_obj.type != "ARMATURE":
            if selected_obj == None:
                box_obj.label(text="no selected object", icon="ERROR")
            elif selected_obj.type != "ARMATURE":
                box_obj.label(
                    text=f"{context.object.name} is not an armature",
                    icon="MOD_WIREFRAME",
                )
            if any(o.type == "ARMATURE" for o in context.scene.objects):
                for a in [o for o in context.scene.objects if o.type == "ARMATURE"]:
                    row_select_armature = box_obj.row()
                    row_select_armature.operator(
                        AVACAPO_OT_create_avacapo.bl_idname,
                        text="",
                        icon="RESTRICT_SELECT_OFF",
                    )
                    row_select_armature.label(text=a.name, icon="OUTLINER_OB_ARMATURE")
            else:
                box_obj.label(text="no armatures", icon="OUTLINER_OB_ARMATURE")
            box_obj.operator(
                AVACAPO_OT_create_avacapo_v1.bl_idname,
                text="New Armature",
                icon="OUTLINER_OB_ARMATURE",
            )
        else:
            # Armature is here
            # -----------------
            armature = context.object
            box_obj.label(text=f"{context.object.name}", icon="OUTLINER_OB_ARMATURE")
            if rig_utils.infer_rig_type(armature) != "avacapo_bvh_v1":
                box_obj.label(text=f"Unknown rig", icon="ERROR")
                box_obj.operator(
                    AVACAPO_OT_create_avacapo.bl_idname,
                    text="Try to Convert",
                    icon="SHADERFX",
                )
            else:
                box_obj.label(text=f"AvaCapo rig v1", icon="CHECKBOX_HLT")
                box_prompt = layout.box()
                row_top = box_prompt.row(align=True)
                col = row_top.row(align=True)
                col.label(text="Start")
                row_top.separator()
                row_top.separator()
                row_top.label(text="Duration", icon="TIME")
                row_top.separator()
                row_top.separator()
                row_top.label(
                    text="End",
                )
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
                # row.prop(settings, "temperature")
                row.prop(settings, "model")
                if State.server_busy:
                    row.label(text="Fetching...", icon="TIME")
                else:
                    if State.server_status:
                        icon = "ERROR" if "Error" in State.server_status else "INFO"
                        text = "Re-Generate"
                    else:
                        text = "Generate"
                        icon = "SHADERFX"
                    row.operator(
                        AVACAPO_OT_fetch.bl_idname,
                        text=text,
                        icon=icon,
                    )
                layout.operator(QUEUE_OT_add_task.bl_idname, text="add Task")
                Queue.draw(layout)


# Registration
# --------------------------------------------------------------------

_classes = [
    # props:
    AvacapoSettings,
    # operators:
    MY_OT_OpenTextPopover,
    QUEUE_OT_add_task,
    AVACAPO_OT_reload_addon,
    AVACAPO_OT_fetch,
    AVACAPO_OT_create_avacapo,
    AVACAPO_OT_create_avacapo_v1,
    # panel:
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
