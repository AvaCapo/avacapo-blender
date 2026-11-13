"""Blender lifecycle hooks for retarget mapping state.

The retarget target is always the active armature. The handler initializes
the mapping when the active armature changes and notices edits to the *rest*
data of armatures that already have a mapping.
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
_ACTIVE_TIMER_KEY = "avacapo_retarget_initialize_active"
_LAST_ACTIVE_KEY = "avacapo_retarget_last_active_armature"
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
    for key in (_INITIALIZATION_TIMER_KEY, _ACTIVE_TIMER_KEY):
        pending = bpy.app.driver_namespace.pop(key, None)
        if pending is not None and bpy.app.timers.is_registered(pending):
            bpy.app.timers.unregister(pending)
    bpy.app.driver_namespace.pop(_LAST_ACTIVE_KEY, None)


def schedule_target_initialization() -> None:
    """Schedule state refresh once normal Blender data access is available."""
    timer = bpy.app.driver_namespace.get(_INITIALIZATION_TIMER_KEY)
    if timer is not None and bpy.app.timers.is_registered(timer):
        return
    bpy.app.driver_namespace[_INITIALIZATION_TIMER_KEY] = _initialize_restored_targets
    bpy.app.timers.register(_initialize_restored_targets, first_interval=0.1)


def _mapped_armatures():
    """Armature objects that already carry an initialized retarget mapping."""
    for obj in bpy.data.objects:
        if obj.type != "ARMATURE":
            continue
        state = getattr(obj, "avacapo_retarget_mapping", None)
        if state is not None and len(state.entries):
            yield obj


def _active_armature():
    view_layer = getattr(bpy.context, "view_layer", None)
    obj = view_layer.objects.active if view_layer is not None else None
    return obj if obj is not None and obj.type == "ARMATURE" else None


@bpy.app.handlers.persistent
def _on_depsgraph_update(_scene, depsgraph) -> None:
    """Initialize a newly active armature and mark mapped rigs dirty on rest edits."""
    try:
        active = _active_armature()
        active_key = active.as_pointer() if active is not None else None
        if active_key != bpy.app.driver_namespace.get(_LAST_ACTIVE_KEY):
            bpy.app.driver_namespace[_LAST_ACTIVE_KEY] = active_key
            if active is not None:
                _schedule_active_initialization()

        changed_data = {
            _original_id(update.id)
            for update in depsgraph.updates
            if isinstance(getattr(update, "id", None), bpy.types.Armature)
        }
        if not changed_data:
            return

        dirty = False
        for armature in _mapped_armatures():
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


def _schedule_active_initialization() -> None:
    timer = bpy.app.driver_namespace.get(_ACTIVE_TIMER_KEY)
    if timer is not None and bpy.app.timers.is_registered(timer):
        return
    bpy.app.driver_namespace[_ACTIVE_TIMER_KEY] = _initialize_active_armature
    bpy.app.timers.register(_initialize_active_armature, first_interval=0.0)


def _initialize_active_armature():
    """Initialize/revalidate the mapping of the armature that just became active."""
    bpy.app.driver_namespace.pop(_ACTIVE_TIMER_KEY, None)
    try:
        armature = _active_armature()
        if armature is not None:
            _initialize(armature, "initializing active armature mapping")
    except Exception:
        logger.log.exception("Unexpected AvaCapo retarget active-armature timer failure")
    return None


def _initialize(armature, operation: str) -> None:
    state = armature.avacapo_retarget_mapping
    try:
        blender_retarget_service.initialize_target_mapping(armature, state)
    except ValueError as error:
        state.draft_error = str(error)
    except Exception:
        _record_unexpected_failure(state, operation)


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
        for armature in tuple(_mapped_armatures()):
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
    """Refresh persisted mapping state only after Blender data is accessible."""
    try:
        bpy.data.objects
    except AttributeError:
        # During extension enable Blender temporarily exposes RestrictData.
        # Keep the same registered timer and try again after registration.
        return 0.1

    bpy.app.driver_namespace.pop(_INITIALIZATION_TIMER_KEY, None)
    try:
        targets = {armature.as_pointer(): armature for armature in _mapped_armatures()}
        active = _active_armature()
        if active is not None:
            targets[active.as_pointer()] = active
        bpy.app.driver_namespace[_LAST_ACTIVE_KEY] = (
            active.as_pointer() if active is not None else None
        )
        for armature in targets.values():
            _initialize(armature, "restoring retarget state")
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
