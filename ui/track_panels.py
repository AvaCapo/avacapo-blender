"""Blender menus, take list and sidebar for timeline clips."""

import math

from bpy.types import Menu, Panel

from ..animation.retargeting import blender_retarget_service
from ..core import takes, tracks
from ..core.storage import Storage
from ..timeline.properties import find_track
from ..timeline.runtime import runtime
from . import ui_retargeting


class CLIPS_MT_empty(Menu):
    bl_label = "Clips"

    def draw(self, context):
        layout = self.layout
        frame = math.floor(runtime.menu_target.frame)
        layout.operator("clips.create", text="Create Clip",
                        icon='ADD').frame = frame
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
        previous, following = tracks.neighbors(
            track.clips, runtime.menu_target.clip_uid)
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
        op = layout.operator("clips.edit_prompt",
                             text="Change Prompt / Settings...")
        op.clip_uid = uid
        layout.menu("CLIPS_MT_takes", text="Change Take")
        layout.menu("CLIPS_MT_stick", text="Stick to Neighbors")
        layout.separator()
        layout.operator("clips.copy_clip", text="Copy Clip",
                        icon='COPYDOWN').clip_uid = uid
        layout.operator("clips.bake_clip", text="Bake Clip",
                        icon='ACTION').clip_uid = uid
        layout.operator("clips.delete_takes",
                        text="Delete All Takes").clip_uid = uid
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
        data = scene.clip_data

        if Storage.api_token:
            row = layout.row(align=True)
            row.label(text="Connected", icon="CHECKMARK")
            row.operator("avacapo.disconnect", text="", icon="X")
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
            row.prop(data, "token_input", text="")
            row.operator("avacapo.paste_token",
                         text="Connect", icon="CHECKMARK")

        # Armature
        armature_col = layout.column(align=True)
        armature_box = armature_col.box()
        armature_box.label(text="Armature:")

        armature = context.active_object
        if armature is None or armature.type != "ARMATURE" or not armature.select_get():
            armature_box.label(text="Select an armature", icon="INFO")
            armature_box.operator(
                "avacapo.append_avacapo_reference_armature",
                text="New Armature",
                icon="OUTLINER_OB_ARMATURE",
            )
            return

        armature_box.label(
            text=armature.name,
            icon="OUTLINER_OB_ARMATURE",
        )
        retarget_box = armature_col.box()
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

        new_clip_box = layout.box()
        new_clip_box.label(text="New clip")
        new_clip_box.prop(data, "default_length")
        new_clip_box.operator("clips.create", text="New Clip at Current Frame",
                              icon='ADD').frame = scene.frame_current

        track = find_track(context)
        clip = tracks.active_clip(track) if track is not None else None

        clip_col = layout.column(align=True)

        if clip is None:
            clip_col.label(text="Select a clip in the Timeline")

            return

        generation_box = clip_col.box()
        generation_box.label(text="Current Clip:")
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
            col = generation_box.column()
            col.prop(clip, "generation_model")
            col.prop(clip, "seed")
            col.prop(clip, "generation_temperature")
            row = col.row(align=True)
            row.prop(clip, "generation_cycled")
            row.prop(clip, "generation_in_place")

        # Takes
        take = takes.current_take(clip)
        takes_box = clip_col.box()
        row = takes_box.row()
        row.prop(
            clip,
            "show_takes",
            text="Takes",
            emboss=False,
            icon='TRIA_DOWN' if clip.show_takes else 'TRIA_RIGHT',
        )
        if clip.show_takes:
            for take in clip.takes:
                row = takes_box.row(align=True)
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
                op = delete_column.operator(
                    "clips.delete_take", text="", icon='TRASH')
                op.clip_uid, op.take_uid = clip.uid, take.uid
                if take is not None:
                    takes_box.prop(take, "Take offset")
            if len(clip.takes) > 0:
                takes_box.operator("clips.delete_takes").clip_uid = clip.uid
            else:
                takes_box.label(text="No Takes Yet, Generate Take")

        # Clip Props
        clip_prop_box = clip_col.box()
        clip_prop_box.prop(clip, "name")

        col = clip_prop_box.column(align=True)
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

        clip_prop_box.operator("clips.delete_clip",
                               icon='TRASH').clip_uid = clip.uid
