bl_info = {
   "name": "AvaCapo AI animation",
    "author": "agamurian",
    "version": (0, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Avacapo",
    "description": "Automating charecter animation with AI",
    "category": "Animation",
}

import bpy
from bpy.types import Operator, Panel

class AVACAPO_OT_create_avacapo(Operator):
    """Create an avacapo"""
    bl_idname = "avacapo.create_avacapo"
    bl_label = "Create avacapo"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        self.report({"INFO"}, "avacapo created!")
        return {"FINISHED"}

    def invoke(self, context, event: bpy.types.Event) -> set[str]:
        return self.execute(context)


class AVACAPO_PT_main_panel(Panel):
    """Main avacapo panel in the N panel"""
    bl_label = "Avacapo"
    bl_idname = "AVACAPO_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Avacapo"

    def draw(self, context) -> None:
        layout = self.layout
        layout.operator(
            AVACAPO_OT_create_avacapo.bl_idname,
            text="Create avacapo",
            icon="MESH_UVSPHERE",
        )


_classes = [
    AVACAPO_OT_create_avacapo,
    AVACAPO_PT_main_panel,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

