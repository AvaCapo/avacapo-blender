"""Blender menus, take list and sidebar for timeline clips."""

import math

from bpy.types import Menu, Panel

from ..animation.retargeting import blender_retarget_service
from ..core import takes, tracks
from ..timeline.properties import find_track
from ..timeline.runtime import runtime
from . import ui_retargeting


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
        track = find_track(context)
        if track is None or track.uid != runtime.menu_target.track_uid:
            self.layout.label(text="No takes")
            return
        clip = tracks.find_clip(track.clips, runtime.menu_target.clip_uid)
        if clip is None or not clip.takes:
            self.layout.label(text="No takes")
            return
        for take in clip.takes:
            op = self.layout.operator(
                "clips.set_take", text=take.name,
                icon='CHECKMARK' if take.uid == clip.active_take_uid else 'BLANK1')
            op.clip_uid, op.take_uid = clip.uid, take.uid


class CLIPS_MT_stick(Menu):
    bl_label = "Stick to Neighbors"

    def draw(self, context):
        track = find_track(context)
        if track is None or track.uid != runtime.menu_target.track_uid:
            return
        previous, following = tracks.neighbors(track.clips, runtime.menu_target.clip_uid)
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
        op.clip_uid = uid
        layout.menu("CLIPS_MT_takes", text="Change Take")
        layout.menu("CLIPS_MT_stick", text="Stick to Neighbors")
        layout.separator()
        layout.operator("clips.copy_clip", text="Copy Clip",
                        icon='COPYDOWN').clip_uid = uid
        layout.operator("clips.bake_clip", text="Bake Clip",
                        icon='ACTION').clip_uid = uid
        layout.separator()
        layout.operator("clips.delete_takes", text="Delete All Takes").clip_uid = uid
        layout.operator("clips.delete_clip", text="Delete Clip",
                        icon='TRASH').clip_uid = uid


class CLIPS_PT_panel(Panel):
    bl_idname = "CLIPS_PT_panel"
    bl_label = "Clips"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clips"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        armature = context.active_object
        if armature is None or armature.type != 'ARMATURE' or not armature.select_get():
            layout.label(text="Select an armature", icon='INFO')
            return

        data = scene.clip_data
        retarget_box = layout.box()
        retarget_ready = (
            hasattr(armature, "avacapo_retarget_mapping")
            and blender_retarget_service.is_valid_mapping(armature)
        )
        retarget_button = retarget_box.row()
        retarget_button.alert = not retarget_ready
        retarget_button.prop(
            data,
            "show_retargeting",
            text="Set up retargeting",
            toggle=True,
            icon='TRIA_DOWN' if data.show_retargeting else 'TRIA_RIGHT',
        )
        if data.show_retargeting:
            ui_retargeting.draw_retarget_mapping(retarget_box, context)

        layout.prop(data, "default_length")
        layout.operator("clips.create", text="New Clip at Current Frame",
                        icon='ADD').frame = scene.frame_current

        track = find_track(context)
        clip = tracks.active_clip(track) if track is not None else None
        if clip is None:
            layout.label(text="Select a clip in the Timeline")
            return

        generation_box = layout.box()
        generation_box.label(text="Prompt")
        generation_box.prop(clip, "prompt", text="")
        generation_box.operator(
            "clips.new_take", text="Generate Take", icon='SHADERFX', depress=True,
        ).clip_uid = clip.uid
        row = generation_box.row()
        row.prop(
            clip,
            "show_advanced_generation",
            text="Advanced Generation Settings",
            emboss=False,
            icon='TRIA_DOWN' if clip.show_advanced_generation else 'TRIA_RIGHT',
        )
        if clip.show_advanced_generation:
            col = generation_box.column(align=True)
            col.prop(clip, "generation_model")
            col.prop(clip, "seed")
            col.prop(clip, "generation_temperature")
            row = col.row(align=True)
            row.prop(clip, "generation_cycled")
            row.prop(clip, "generation_in_place")

        box = layout.box()
        box.prop(clip, "name")

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
        take = takes.current_take(clip)
        if take is not None:
            col.prop(take, "offset")

        box = layout.box()
        row = box.row()
        row.prop(
            clip,
            "show_takes",
            text="Takes",
            emboss=False,
            icon='TRIA_DOWN' if clip.show_takes else 'TRIA_RIGHT',
        )
        if clip.show_takes:
            for take in clip.takes:
                row = box.row(align=True)
                columns = row.split(factor=0.9, align=True)
                name_column = columns.column()
                delete_column = columns.column()
                name_column.alignment = 'LEFT'
                op = name_column.operator(
                    "clips.set_take",
                    text=take.name,
                    depress=take.uid == clip.active_take_uid,
                )
                op.clip_uid, op.take_uid = clip.uid, take.uid
                op = delete_column.operator("clips.delete_take", text="", icon='TRASH')
                op.clip_uid, op.take_uid = clip.uid, take.uid
            box.operator("clips.delete_takes").clip_uid = clip.uid

        layout.operator("clips.delete_clip", icon='TRASH').clip_uid = clip.uid
