from bpy.types import Operator, Panel
import bpy
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


class MySettings(bpy.types.PropertyGroup):
    text_block: bpy.props.PointerProperty(type=bpy.types.Text)
    my_text: bpy.props.StringProperty(
        name="",
        default="",
        description="Enter text here"
    )
    start: bpy.props.IntProperty(
        name="start",
        default=0,
    )
    duration: bpy.props.FloatProperty(
        name="duration",
        default=2.5,
    )
    end: bpy.props.IntProperty(
        name="end",
        default=120,
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="generation model",
        items=[
            ('ASM', "Asm", "Fast simple ASM"),
            ('GEN1', "Gen 1", "First generation GPAT"),
            ('GEN2', "Gen 2", "Second generation GPAT"),
        ],
        default='ASM'
    )


# Operators:
# --------------------------------------------------------------------
class AVACAPO_OT_create_avacapo(Operator):
    """Create an avacapo"""
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
        settings = context.scene.my_settings
        text = settings.my_text

        row = layout.row()
        row.label(text="Preview:", icon='TEXT')
        row.label(text=context.object.name,
                  icon="OUTLINER_OB_ARMATURE")
        row.label(text="5s",
                  icon="TIME")
        box = layout.box()

        if text:
            # Split by newlines and display each line
            for line in text.split("\n"):
                box.label(text=line if line else " ")
        else:
            box.label(text="(empty)", icon='INFO')
        layout.operator(
            AVACAPO_OT_create_avacapo.bl_idname,
            text="Generate",
            icon="SHADERFX",
        )
        layout.prop(settings, "model")

    def execute(self, context):
        return {'FINISHED'}

# UTILS
# --------------------------------------------------------------------


def check_type_of_armature(armature):
    return "AVACAPO_V1"


def get_selected_obj(context) -> bpy.types.Object | None:
    return context.object

# Panel
# --------------------------------------------------------------------


class AVACAPO_PT_main_panel(Panel):
    """Main avacapo panel in the N panel"""
    bl_label = "Avacapo"
    bl_idname = "AVACAPO_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Avacapo"

    def draw(self, context) -> None:
        layout = self.layout
        row = layout.row(align=True)
        row.label(text="Connected", icon="INTERNET")
        row.operator(
            AVACAPO_OT_create_avacapo.bl_idname,
            text="",
            icon="MESH_UVSPHERE",
        )
        box_obj = layout.box()
        settings = context.scene.my_settings

        selected_obj = get_selected_obj(context)
        # for objects every redraw!!!
        if selected_obj == None:
            box_obj.label(text='no selected object',
                          icon="ERROR")
        elif selected_obj.type != 'ARMATURE':
            box_obj.label(text=f"{context.object.name} is not an armature",
                          icon="MOD_WIREFRAME")
            if any(o.type == "ARMATURE" for o in context.scene.objects):
                for a in [o for o in context.scene.objects if o.type == "ARMATURE"]:
                    row_select_armature = box_obj.row()
                    row_select_armature.operator(
                        AVACAPO_OT_create_avacapo.bl_idname,
                        text="",
                        icon="RESTRICT_SELECT_OFF",
                    )
                    row_select_armature.label(text=a.name,
                                              icon="OUTLINER_OB_ARMATURE")
            else:
                box_obj.label(text="no armatures",
                              icon="OUTLINER_OB_ARMATURE")
                box_obj.operator(
                    AVACAPO_OT_create_avacapo.bl_idname,
                    text="Create Armature",
                    icon="OUTLINER_OB_ARMATURE",
                )
        else:
            # Armature is here
            # -----------------
            armature = context.object
            box_obj.label(text=f"{context.object.name}",
                          icon="OUTLINER_OB_ARMATURE")
            if not check_type_of_armature(armature) == "AVACAPO_V1":
                box_obj.label(text=f"Unknown rig", icon="ERROR")
                box_obj.operator(AVACAPO_OT_create_avacapo.bl_idname,
                                 text="Try to Convert", icon="SHADERFX")
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
                row_top.label(text="End", )
                row = box_prompt.row(align=True)
                row.operator(AVACAPO_OT_create_avacapo.bl_idname,
                             text="", icon="RECORD_ON")
                row.prop(settings, "start", text="", expand=True)
                row.separator()
                row.separator()
                row.prop(settings, "duration",  text="", expand=True)
                row.separator()
                row.separator()
                row.prop(settings, "end", text="", expand=True)
                row.operator(AVACAPO_OT_create_avacapo.bl_idname,
                             text="", icon="RECORD_ON")
                box_prompt.separator(type="LINE")
                row_desc = box_prompt.row(align=True)
                row_desc.label(text="Prompt:", icon="TEXT")
                row_desc.operator("my.open_text_popover",
                                  text="", icon="FULLSCREEN_ENTER")
                row_prompt = box_prompt.row(align=True)
                row_prompt.prop(settings, "my_text")
                row = box_prompt.row(align=True)
                row.prop(settings, "model")
                row.operator(
                    AVACAPO_OT_create_avacapo.bl_idname,
                    text="Generate",
                    icon="SHADERFX",
                )


# Registration
# --------------------------------------------------------------------

_classes = [
    MySettings,
    MY_OT_OpenTextPopover,
    AVACAPO_OT_create_avacapo,
    AVACAPO_PT_main_panel,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.my_settings = bpy.props.PointerProperty(type=MySettings)


def unregister() -> None:
    del bpy.types.Scene.my_settings
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
