"""Blender lifecycle hooks for retarget mapping state.

The handler only notices edits to an assigned target armature's *rest* data.
It never compiles a hierarchy in Blender's depsgraph callback: that work is
debounced into a timer, so panel drawing remains display-only and edit-mode
updates do not repeatedly rebuild retarget state.
"""

from __future__ import annotations

import bpy
import bpy.app.handlers

from ...common import logger
from . import blender_retarget_service


_HANDLER_KEY = "avacapo_retarget_rest_skeleton_handler"
_LOAD_HANDLER_KEY = "avacapo_retarget_load_handler"
_TIMER_KEY = "avacapo_retarget_rest_skeleton_refresh"
_INITIALIZATION_TIMER_KEY = "avacapo_retarget_initialize_targets"
_DEBOUNCE_SECONDS = 0.15


def register() -> None:
    """Install handlers that maintain state after rest edits and file loads."""
    unregister()
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    bpy.app.driver_namespace[_HANDLER_KEY] = _on_depsgraph_update
    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.driver_namespace[_LOAD_HANDLER_KEY] = _on_load_post


def unregister() -> None:
    """Remove this module's handlers and pending refresh timers."""
    for handler in tuple(bpy.app.handlers.depsgraph_update_post):
        if (
            getattr(handler, "__module__", None) == __name__
            and getattr(handler, "__name__", None) == "_on_depsgraph_update"
        ):
            bpy.app.handlers.depsgraph_update_post.remove(handler)
    bpy.app.driver_namespace.pop(_HANDLER_KEY, None)

    for handler in tuple(bpy.app.handlers.load_post):
        if (
            getattr(handler, "__module__", None) == __name__
            and getattr(handler, "__name__", None) == "_on_load_post"
        ):
            bpy.app.handlers.load_post.remove(handler)
    bpy.app.driver_namespace.pop(_LOAD_HANDLER_KEY, None)

    timer = bpy.app.driver_namespace.pop(_TIMER_KEY, None)
    if timer is not None and bpy.app.timers.is_registered(timer):
        bpy.app.timers.unregister(timer)
    initialization_timer = bpy.app.driver_namespace.pop(_INITIALIZATION_TIMER_KEY, None)
    if initialization_timer is not None and bpy.app.timers.is_registered(initialization_timer):
        bpy.app.timers.unregister(initialization_timer)


def schedule_target_initialization() -> None:
    """Schedule state refresh once normal Blender data access is available."""
    timer = bpy.app.driver_namespace.get(_INITIALIZATION_TIMER_KEY)
    if timer is not None and bpy.app.timers.is_registered(timer):
        return
    bpy.app.driver_namespace[_INITIALIZATION_TIMER_KEY] = _initialize_restored_targets
    bpy.app.timers.register(_initialize_restored_targets, first_interval=0.1)


@bpy.app.handlers.persistent
def _on_depsgraph_update(_scene, depsgraph) -> None:
    """Mark selected targets dirty when their armature rest datablock changes."""
    try:
        changed_data = {
            _original_id(update.id)
            for update in depsgraph.updates
            if getattr(update, "id", None) is not None
        }
        if not changed_data:
            return

        dirty = False
        for scene in bpy.data.scenes:
            armature = getattr(scene, "avacapo_retarget_target", None)
            if armature is None or armature.type != "ARMATURE":
                continue
            if armature.data not in changed_data:
                continue
            armature.avacapo_retarget_mapping.rest_skeleton_dirty = True
            dirty = True
        if dirty:
            _schedule_refresh()
    except Exception:
        # A handler exception can otherwise make Blender silently stop calling
        # this watcher. There is no reliable target state to update here.
        logger.log.exception("Unexpected AvaCapo retarget depsgraph watcher failure")


@bpy.app.handlers.persistent
def _on_load_post(_unused) -> None:
    """Refresh persisted target state after a new Blender file has loaded."""
    schedule_target_initialization()


def _schedule_refresh() -> None:
    timer = bpy.app.driver_namespace.get(_TIMER_KEY)
    if timer is not None and bpy.app.timers.is_registered(timer):
        return
    bpy.app.driver_namespace[_TIMER_KEY] = _refresh_dirty_targets
    bpy.app.timers.register(_refresh_dirty_targets, first_interval=_DEBOUNCE_SECONDS)


def _refresh_dirty_targets():
    """Rebuild cached validation/status after Blender has finished the edit."""
    bpy.app.driver_namespace.pop(_TIMER_KEY, None)
    try:
        for scene in bpy.data.scenes:
            armature = getattr(scene, "avacapo_retarget_target", None)
            if armature is None or armature.type != "ARMATURE":
                continue
            state = armature.avacapo_retarget_mapping
            if not state.rest_skeleton_dirty:
                continue
            try:
                joint_map = blender_retarget_service.mapping_dict(state)
                blender_retarget_service.refresh_mapping_state(armature, state, joint_map)
                blender_retarget_service.refresh_active_action_status(armature, state)
                state.draft_error = ""
            except ValueError as error:
                state.validation_ready = False
                state.retarget_action_status = "unknown"
                state.draft_error = str(error)
            except Exception:
                _record_unexpected_failure(state, "refreshing retarget state")
            finally:
                state.rest_skeleton_dirty = False
    except Exception:
        logger.log.exception("Unexpected AvaCapo retarget refresh timer failure")
    return None


def _initialize_restored_targets():
    """Refresh persisted target state only after Blender data is accessible."""
    try:
        scenes = tuple(bpy.data.scenes)
    except AttributeError:
        # During extension enable Blender temporarily exposes RestrictData.
        # Keep the same registered timer and try again after registration.
        return 0.1

    bpy.app.driver_namespace.pop(_INITIALIZATION_TIMER_KEY, None)
    try:
        for scene in scenes:
            armature = getattr(scene, "avacapo_retarget_target", None)
            if armature is None or armature.type != "ARMATURE":
                continue
            state = armature.avacapo_retarget_mapping
            try:
                blender_retarget_service.initialize_target_mapping(armature, state)
            except ValueError as error:
                state.draft_error = str(error)
            except Exception:
                _record_unexpected_failure(state, "restoring retarget state")
    except Exception:
        logger.log.exception("Unexpected AvaCapo retarget restore timer failure")
    return None


def _original_id(value):
    return getattr(value, "original", value)


def _record_unexpected_failure(state, operation: str) -> None:
    """Keep timer errors visible without leaking a traceback into panel text."""
    logger.log.exception("Unexpected AvaCapo retarget watcher failure while %s", operation)
    state.validation_ready = False
    state.retarget_action_status = "unknown"
    state.draft_error = "Unexpected retarget watcher error; see Blender console."
