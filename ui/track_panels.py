"""Blender menus, take list and sidebar for timeline clips."""

import math

from bpy.types import Menu, Panel, UIList

from ..core import takes, tracks
from .track_runtime import runtime
from .track_style import STYLE


class CLIPS_MT_empty(Menu):
    bl_label = "Clips"

    def draw(self, context):
        layout = self.layout
        frame = math.floor(runtime.menu_target.frame)
        layout.operator("clips.create", text="Create Clip", icon='ADD').frame = frame
        layout.operator("clips.fill_gap", text="Fill In").frame = frame
        layout.operator("clips.shrink_neighbors",
                        text="Shrink Neighbors to Current Frame").frame = frame
        if runtime.clipboard:
            layout.separator()
            layout.operator("clips.paste_clip", text="Paste Clip",
                            icon='PASTEDOWN').frame = frame


class CLIPS_MT_takes(Menu):
    bl_label = "Change Take"

    def draw(self, context):
        clip = tracks.find_clip(context.scene.clip_data.clips, runtime.menu_target.clip_uid)
        if clip is None or not clip.takes:
            self.layout.label(text="No takes")
            return
        for index, take in enumerate(clip.takes):
            op = self.layout.operator(
                "clips.set_take", text=f"{take.name}   {round(take.progress * 100)}%",
                icon='CHECKMARK' if index == clip.active_take else 'BLANK1')
            op.clip_uid, op.index = clip.uid, index


class CLIPS_MT_stick(Menu):
    bl_label = "Stick to Neighbors"

    def draw(self, context):
        data = context.scene.clip_data
        previous, following = tracks.neighbors(data.clips, runtime.menu_target.clip_uid)
        has_previous, has_next = previous is not None, following is not None
        for label, mode, enabled in (
            ("Both", 'BOTH', has_previous and has_next),
            ("Next", 'NEXT', has_next),
            ("Previous", 'PREV', has_previous),
        ):
            row = self.layout.row()
            row.enabled = enabled
            op = row.operator("clips.stick", text=label)
            op.clip_uid, op.mode = runtime.menu_target.clip_uid, mode


class CLIPS_MT_clip(Menu):
    bl_label = "Clip"

    def draw(self, context):
        layout = self.layout
        uid = runtime.menu_target.clip_uid
        layout.operator("clips.new_take", text="New Take").clip_uid = uid
        op = layout.operator("clips.edit_prompt", text="Change Prompt / Settings...")
        op.clip_uid, op.make_take = uid, True
        layout.menu("CLIPS_MT_takes", text="Change Take")
        layout.menu("CLIPS_MT_stick", text="Stick to Neighbors")
        layout.separator()
        layout.operator("clips.copy_clip", text="Copy Clip",
                        icon='COPYDOWN').clip_uid = uid
        layout.separator()
        layout.operator("clips.delete_takes", text="Delete All Takes").clip_uid = uid
        layout.operator("clips.delete_clip", text="Delete Clip",
                        icon='TRASH').clip_uid = uid


class CLIPS_UL_takes(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name)
        row.label(text=f"{round(item.progress * 100)}%")


class CLIPS_PT_panel(Panel):
    bl_idname = "CLIPS_PT_panel"
    bl_label = "Clips"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clips"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        clip_data = scene.clip_data
        layout.prop(clip_data, "default_length")
        layout.operator("clips.create", text="New Clip at Current Frame",
                        icon='ADD').frame = scene.frame_current

        clip = tracks.active_clip(clip_data)
        if clip is None:
            layout.label(text="Select a clip in the Timeline")
            return

        box = layout.box()
        box.prop(clip, "name")
        box.prop(clip, "prompt")
        box.prop(clip, "seed")

        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(clip, "start")
        op = row.operator("clips.set_frame", text="", icon='TIME')
        op.clip_uid, op.which = clip.uid, 'START'
        row.prop(clip, "bind_start", text="", toggle=True,
                 icon='LINKED' if clip.bind_start else 'UNLINKED')
        row = col.row(align=True)
        row.prop(clip, "end")
        op = row.operator("clips.set_frame", text="", icon='TIME')
        op.clip_uid, op.which = clip.uid, 'END'
        row.prop(clip, "bind_end", text="", toggle=True,
                 icon='LINKED' if clip.bind_end else 'UNLINKED')
        col.prop(clip, "duration")

        box = layout.box()
        box.label(text="Takes")
        box.template_list("CLIPS_UL_takes", "", clip, "takes", clip, "active_take",
                          rows=STYLE.take_list_rows)
        take = takes.current_take(clip)
        if take is not None:
            col = box.column(align=True)
            col.prop(take, "offset")
            col.prop(take, "length")
            col.prop(take, "progress", slider=True)
        row = box.row(align=True)
        row.operator("clips.new_take", text="New Take").clip_uid = clip.uid
        op = row.operator("clips.edit_prompt", text="Prompt...")
        op.clip_uid, op.make_take = clip.uid, True
        box.operator("clips.delete_takes").clip_uid = clip.uid

        layout.operator("clips.delete_clip", icon='TRASH').clip_uid = clip.uid
