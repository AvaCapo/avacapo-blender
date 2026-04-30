# all ui and draws:
import bpy

from dataclasses import dataclass

from .storage import Storage
from . import rig_utils
from .server import get_fps
from .state_controller import State, Queue, STATUS_META


class AVACAPO_PT_main_panel(bpy.types.Panel):
    """Main avacapo panel in the N panel"""

    bl_label = "Avacapo"
    bl_idname = "AVACAPO_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Avacapo"

    def draw(self, context) -> None:
        layout = self.layout
        settings = context.scene.avacapo_settings

        # --- Auth Section ---
        if Storage.api_token != "":
            row = layout.row(align=True)
            row.label(text="Connected", icon="CHECKMARK")
            row.operator(
                "avacapo.disconnect",
                text="",
                icon="X",
            )
            row.operator(
                "avacapo.reload_addon",
                text="",
                icon="MESH_UVSPHERE",
            )
        else:
            box_auth = layout.box()
            box_auth.label(text="Not Connected", icon="ERROR")
            box_auth.operator(
                "avacapo.login_browser",
                text="Login via Browser",
                icon="URL",
            )
            box_auth.separator()
            box_auth.label(text="Or paste token manually:")
            row = box_auth.row(align=True)
            row.prop(settings, "token_input", text="")
            row.operator(
                "avacapo.paste_token",
                text="Connect",
                icon="CHECKMARK",
            )
            row_reload = layout.row(align=True)
            row_reload.operator(
                "avacapo.reload_addon",
                text="",
                icon="MESH_UVSPHERE",
            )
            return  # Don't show generation UI when disconnected

        # --- Generation Section ---
        box_obj = layout.box()

        selected_obj = context.object
        if selected_obj is None or selected_obj.type != "ARMATURE":
            if selected_obj is None:
                box_obj.label(text="No selected object", icon="ERROR")
            elif selected_obj.type != "ARMATURE":
                box_obj.label(
                    text=f"{context.object.name} is not an armature",
                    icon="MOD_WIREFRAME",
                )
            armatures = [o for o in context.scene.objects if o.type == "ARMATURE"]
            if armatures:
                for armature in armatures:
                    row_select_armature = box_obj.row()
                    op = row_select_armature.operator(
                        "avacapo.select_by_name",
                        text="",
                        icon="RESTRICT_SELECT_OFF",
                    )
                    op.obj_name = armature.name
                    row_select_armature.label(text=armature.name, icon="OUTLINER_OB_ARMATURE")
            else:
                box_obj.label(text="no armatures", icon="OUTLINER_OB_ARMATURE")
            box_obj.operator(
                "avacapo.create_avacapo_v1",
                text="New Armature",
                icon="OUTLINER_OB_ARMATURE",
            )

        else:
            armature = context.object
            box_obj.label(text=f"{context.object.name}", icon="OUTLINER_OB_ARMATURE")
            if rig_utils.infer_rig_type(armature) != "avacapo_bvh_v1":
                box_obj.label(text="Unknown rig", icon="ERROR")
                box_obj.operator(
                    "avacapo.create_avacapo",
                    text="Try to Convert",
                    icon="SHADERFX",
                )
            else:
                box_obj.label(text="AvaCapo rig v1", icon="CHECKBOX_HLT")
                box_prompt = layout.box()
                row_top = box_prompt.row(align=True)
                col = row_top.row(align=True)
                col.label(text="Start", icon="KEYFRAME")
                row_top.separator()
                row_top.separator()
                row_top.label(text="Seconds", icon="TIME")
                row_top.separator()
                row_top.separator()
                row_top.label(text="End", icon="KEYFRAME")
                row = box_prompt.row(align=True)
                record_icon = "RECORD_ON" if settings.start_record_lock else "RECORD_OFF"
                row.operator("avacapo.toggle_start_record_lock", text="", icon=record_icon)
                row.prop(settings, "start", text="", expand=True)
                row.separator()
                row.separator()
                row.prop(settings, "duration", text="", expand=True)
                row.separator()
                row.separator()
                row.prop(settings, "end", text="", expand=True)
                # row.operator("avacapo.create_avacapo", text="", icon="RECORD_ON")
                # ommit last frame lock for now
                # box_prompt.label(text=f"frame rate {get_fps()}")
                box_prompt.separator(type="LINE")
                row_desc = box_prompt.row(align=True)
                row_desc.label(text="Prompt:", icon="TEXT")
                row_prompt = box_prompt.row(align=True)
                row_prompt.prop(settings, "prompt")
                row = box_prompt.row(align=True)
                row.prop(settings, "model")

                # Generate button — now queues a task instead of fetching directly
                if not Queue.allow_new_task:
                    row.label(text="Queue is full...", icon="TIME")
                else:
                    row.operator(
                        "queue.add_task",
                        text="Generate",
                        icon="SHADERFX",
                    )

                if "Error" in State.server_status:
                    layout.label(text=State.server_status, icon="ERROR")

                if layout is not None:
                    draw_queue(layout)
                    draw_clips(layout)


def draw_queue_task(layout: bpy.types.UILayout, task: "Queue.Task") -> None:
    box = layout.box()

    row = box.row(align=True)
    icon, status_label = STATUS_META.get(task.status, ("QUESTION", task.status))
    row.label(text=f"[{task.id}]")
    row.label(text=status_label, icon=icon)
    if task.generation_time > 0:
        row.label(text=f"{task.generation_time:.1f}s", icon="TEMP")

    row2 = box.row()
    prompt_preview = task.prompt[:48] + ("..." if len(task.prompt) > 48 else "")
    row2.label(text=prompt_preview, icon="TEXT")

    row3 = box.row(align=True)
    row3.label(text=f"frame {task.start_frame}", icon="KEYFRAME")
    row3.label(text=f"{task.duration}s", icon="TIME")
    row3.label(text=task.model, icon="SHADERFX")

    if task.status in Queue.TERMINAL_STATUSES:
        row4 = box.row(align=True)
        redo = row4.operator("queue.redo_task", text="Redo", icon="FILE_REFRESH")
        redo.task_id = task.id
        discard = row4.operator("queue.discard_task", text="", icon="X")
        discard.task_id = task.id


def draw_queue(layout: bpy.types.UILayout) -> None:
    for task in Queue.tasks:
        draw_queue_task(layout, task)


@dataclass
class Clip:
    name: str


default_clip = Clip(name="Walking backwards")
clips = [default_clip]
CLIP_ICON = "RENDER_ANIMATION"


def draw_clips(layout: bpy.types.UILayout) -> None:
    if not clips:
        layout.label(text="No clips.", icon=CLIP_ICON)
        return
    for clip in clips:
        draw_clip(layout, clip)


def draw_clip(layout: bpy.types.UILayout, clip=default_clip) -> None:
    box = layout.box()
    box.label(text=clip.name, icon=CLIP_ICON)
