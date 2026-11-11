"""Blender menus, take list and sidebar for timeline clips."""

import math

from ..timeline.generation_status import is_pending, STATUS_LABELS
from bpy.types import Menu, Panel

from ..core import takes, tracks
from ..core.generation_models import TaskStatus
from ..core.storage import Storage
from ..timeline.properties import find_track
from ..timeline.runtime import runtime
from . import ui_retargeting


class CLIPS_MT_empty(Menu):
    bl_label = "Clips"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'EXEC_DEFAULT'
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
            row = self.layout.row()
            row.enabled = take.take_action is not None
            op = row.operator(
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
        track = find_track(context)
        clip = tracks.find_clip(track.clips, uid) if track is not None else None
        row = layout.row()
        row.enabled = clip is not None
        row.operator_context = 'INVOKE_DEFAULT'
        operator = "clips.new_take" if clip is not None and clip.prompt.strip() else "clips.edit_prompt"
        row.operator(operator, text="Generate Take").clip_uid = uid
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

# the main and only panel
class CLIPS_PT_panel(Panel):
    bl_idname = "CLIPS_PT_panel"
    bl_label = "AvaCapo"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AvaCapo"

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
        layout.separator(type='LINE')
        armature_col = layout.column(align=True)

        armature = context.active_object
        if armature is None or armature.type != "ARMATURE" or not armature.select_get():
            armature_col.label(text="Select an armature", icon="INFO")
            armature_col.operator(
                "avacapo.append_avacapo_reference_armature",
                text="New Armature",
                icon="OUTLINER_OB_ARMATURE",
            )
            return

        armature_col.label(
            text=f'{armature.name}:',
            icon="OUTLINER_OB_ARMATURE",
        )
        retarget_header, retarget_body = layout.panel(
            "avacapo_retargeting", default_closed=True,
        )
        _, status_icon = ui_retargeting.retarget_mapping_status(armature)
        retarget_header.alert = status_icon == 'ERROR'
        retarget_header.label(text="Set up retargeting")
        status = retarget_header.row(align=True)
        status.alignment = 'RIGHT'
        status.operator(
            "avacapo.retarget_mapping_status", text="",
            icon=status_icon, emboss=False,
        )
        if retarget_body is not None:
            ui_retargeting.draw_retarget_mapping(retarget_body, context)

        layout.separator(type='LINE')
        layout.label(text="New Clip:")
        new_clip_col = layout.column(align=True)
        new_clip_col.prop(data, "default_length", text="Frames")
        row = new_clip_col.row()
        row.operator_context = 'EXEC_DEFAULT'
        op = row.operator("clips.create", text="New from Current Frame", icon='ADD')
        op.frame = scene.frame_current

        track = find_track(context)
        clip = tracks.active_clip(track) if track is not None else None

        layout.separator(type='LINE')
        layout.label(text="Current Clip:")
        if clip is None:
            hint = layout.column(align=True)
            hint.label(text="Select a clip on the timeline")
            hint.label(text="or create a new clip")

            return

        generation_col = layout.column()
        generation_col.prop(clip, "prompt", text="", placeholder="prompt")
        row = generation_col.row()
        row.enabled = bool(clip.prompt.strip())
        row.active_default = True
        row.operator(
            "clips.new_take", text="Generate Take", icon='SHADERFX',
        ).clip_uid = clip.uid
        header, body = layout.panel(
            "avacapo_advanced_generation", default_closed=True,
        )
        header.label(text="Advanced Generation Settings")
        if body is not None:
            col = body.column(align=True)
            col.prop(clip, "generation_model")
            col.separator()
            row = col.row(align=True)
            seed = row.row(align=True)
            seed.enabled = not clip.randomize_seed
            seed.prop(clip, "seed")
            row.prop(clip, "randomize_seed", text="", icon='TIME', toggle=True)
            col.prop(clip, "generation_temperature")
            row = col.row(align=True)
            row.prop(clip, "generation_cycled")
            row.prop(clip, "generation_in_place")
            col.prop(clip, "generation_enhance_prompt")

        # Takes
        header, body = layout.panel("avacapo_takes", default_closed=False)
        header.label(text="Takes")
        if body is not None:
            take_list = body.column(align=True)
            for take in clip.takes:
                row = take_list.row(align=True)
                name_row = row
                if take.take_action is None:
                    name_row = row.row(align=True)
                    name_row.enabled = False
                status = TaskStatus(take.generation_status)
                op = name_row.operator(
                    "clips.set_take",
                    text=(take.name if status == TaskStatus.SUCCEEDED
                          else f"{take.name} - {STATUS_LABELS[status]}"),
                    icon='RADIOBUT_ON' if take.uid == clip.active_take_uid else 'BLANK1',
                )
                op.clip_uid, op.take_uid = clip.uid, take.uid
                pending = is_pending(take)
                op = row.operator(
                    "clips.cancel_take" if pending else "clips.delete_take",
                    text="", icon='CANCEL' if pending else 'TRASH')
                op.clip_uid, op.take_uid = clip.uid, take.uid
                if take.generation_error:
                    take_list.label(text=take.generation_error, icon='ERROR')
            if len(clip.takes) > 0:
                body.operator("clips.delete_takes").clip_uid = clip.uid
            else:
                body.label(text="No Takes Yet, Generate Take")

        # Clip Props
        header, body = layout.panel("avacapo_clip_settings", default_closed=True)
        header.label(text="Clip Settings")
        if body is None:
            return
        body.prop(clip, "name")
        col = body.column(align=True)
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
        if take is not None and take.take_action is not None:
            body.prop(take, "offset", text="Take Offset (frames)")

        body.operator("clips.delete_clip", icon='TRASH').clip_uid = clip.uid
