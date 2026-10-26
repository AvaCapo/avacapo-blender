"""Register Blender UI resources for the add-on."""

import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty

from .track_generation import TIMER_CALLBACKS, cancel_generation
from ..timeline.drawing import draw_callback
from ..timeline.interaction import CLIPS_OT_hover, CLIPS_OT_mouse
from ..timeline.operators import (
    CLIPS_OT_create, CLIPS_OT_fill_gap,
    CLIPS_OT_shrink_neighbors, CLIPS_OT_edit_prompt, CLIPS_OT_new_take,
    CLIPS_OT_delete_takes, CLIPS_OT_delete_take, CLIPS_OT_delete_clip, CLIPS_OT_bake_clip,
    CLIPS_OT_copy_clip,
    CLIPS_OT_paste_clip,
    CLIPS_OT_set_take, CLIPS_OT_stick, CLIPS_OT_set_frame,
)
from .track_panels import (
    CLIPS_MT_empty, CLIPS_MT_takes, CLIPS_MT_stick, CLIPS_MT_clip, CLIPS_PT_panel,
)
from ..timeline.properties import CT_Take, CT_Clip, CT_Track, CT_Scene
from ..timeline.runtime import runtime
from .update_notice import TIMER_CALLBACKS as UPDATE_TIMER_CALLBACKS, stop_update_notice
from . import ui_retargeting
from ..animation.retargeting import blender_retarget_watcher


CLASSES = (
    CT_Take, CT_Clip, CT_Track, CT_Scene,
    CLIPS_OT_hover, CLIPS_OT_mouse, CLIPS_OT_create, CLIPS_OT_fill_gap,
    CLIPS_OT_shrink_neighbors, CLIPS_OT_edit_prompt, CLIPS_OT_new_take,
    CLIPS_OT_delete_takes, CLIPS_OT_delete_take, CLIPS_OT_delete_clip, CLIPS_OT_bake_clip,
    CLIPS_OT_copy_clip,
    CLIPS_OT_paste_clip,
    CLIPS_OT_set_take, CLIPS_OT_stick, CLIPS_OT_set_frame,
    CLIPS_MT_empty, CLIPS_MT_takes, CLIPS_MT_stick, CLIPS_MT_clip, CLIPS_PT_panel,
)


_STATE_KEY = "timeline_clips_state"
_LEGACY_STATE_KEY = "timeline_clip_track_state"
_registration_state = globals().get("_registration_state")


def _state():
    global _registration_state
    namespace = bpy.app.driver_namespace
    if _STATE_KEY in namespace:
        _registration_state = namespace[_STATE_KEY]
    elif _registration_state is None:
        _registration_state = {
            "handle": None, "keymaps": [], "classes": [], "timers": TIMER_CALLBACKS,
            "reset_runtime": runtime.reset,
            "update_timers": UPDATE_TIMER_CALLBACKS,
        }
    # driver_namespace is reset when a .blend is loaded; keep the resource registry alive.
    namespace[_STATE_KEY] = _registration_state
    return _registration_state


@persistent
def _on_load_pre(_):
    cancel_generation(_state().get("timers", set()))
    stop_update_notice(_state().get("update_timers", set()))


@persistent
def _on_load_post(_):
    _state()


def _unregister_retargeting():
    """Remove Retarget handlers before their UI properties and classes."""
    blender_retarget_watcher.unregister()
    ui_retargeting.unregister()


def unregister():
    _unregister_retargeting()
    states = [_state()]
    legacy = bpy.app.driver_namespace.get(_LEGACY_STATE_KEY)
    if legacy is not None and legacy is not states[0]:
        states.append(legacy)
    for state in states:
        cancel_generation(state.get("timers", set()))
        stop_update_notice(state.get("update_timers", set()))
        for handler_list, callback in state.get("handlers", []):
            if callback in handler_list:
                handler_list.remove(callback)
        state.get("handlers", []).clear()
        if state.get("handle") is not None:
            try:
                bpy.types.SpaceDopeSheetEditor.draw_handler_remove(state["handle"], 'WINDOW')
            except (RuntimeError, ValueError, ReferenceError):
                pass
            state["handle"] = None
        for keymap, item in state.get("keymaps", []):
            try:
                keymap.keymap_items.remove(item)
            except (RuntimeError, ReferenceError):
                pass
        state.get("keymaps", []).clear()
        if state.get("reset_runtime"):
            state["reset_runtime"]()

    # Remove pointers before unregistering the PropertyGroups they reference.
    for property_name in ("clip_data", "clip_track"):
        if hasattr(bpy.types.Scene, property_name):
            delattr(bpy.types.Scene, property_name)
    for state in states:
        for cls in reversed(state.get("classes", [])):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        state.get("classes", []).clear()
    bpy.app.driver_namespace.pop(_LEGACY_STATE_KEY, None)
    runtime.reset()


def register():
    unregister()
    state = _state()
    state.update(
        timers=TIMER_CALLBACKS,  reset_runtime=runtime.reset,
        update_timers=UPDATE_TIMER_CALLBACKS,
    )
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            state["classes"].append(cls)
        bpy.types.Scene.clip_data = PointerProperty(type=CT_Scene)
        state.setdefault("handlers", [])
        for handler_list, callback in (
            (bpy.app.handlers.load_pre, _on_load_pre),
            (bpy.app.handlers.load_post, _on_load_post),
        ):
            handler_list.append(callback)
            state["handlers"].append((handler_list, callback))
        state["handle"] = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
            draw_callback, (), 'WINDOW', 'POST_PIXEL',
        )
        keyconfig = bpy.context.window_manager.keyconfigs.addon
        if keyconfig is not None:
            keymap = keyconfig.keymaps.new(name="Dopesheet", space_type='DOPESHEET_EDITOR')
            for operator, button, value in (
                ("clips.mouse", 'LEFTMOUSE', 'PRESS'),
                ("clips.mouse", 'LEFTMOUSE', 'DOUBLE_CLICK'),
                ("clips.mouse", 'RIGHTMOUSE', 'PRESS'),
                ("clips.mouse", 'RIGHTMOUSE', 'DOUBLE_CLICK'),
                ("clips.mouse", 'W', 'PRESS'),
                ("clips.hover", 'MOUSEMOVE', 'ANY'),
            ):
                item = keymap.keymap_items.new(operator, button, value)
                state["keymaps"].append((keymap, item))
        ui_retargeting.register()
        blender_retarget_watcher.register()
        blender_retarget_watcher.schedule_target_initialization()
    except Exception:
        unregister()
        raise
