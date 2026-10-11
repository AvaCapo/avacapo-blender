"""Extension entry point and UI lifecycles."""

from __future__ import annotations

import importlib
import traceback

import bpy

from .ui import ui_tracks
from .animation import animation_utils
from .animation.retargeting import (
    blender_adapter,
    blender_artifacts,
    blender_debug,
    blender_debug_compare,
    blender_source_adapter,
    blender_ui,
    mapping,
    workflow,
)


_TRACKS_TIMER_KEY = "avacapo_deferred_timeline_tracks_launch"


def _launch_timeline_tracks_after_registration():
    """Start the context-dependent timeline UI without hiding Retarget UI on failure."""
    try:
        ui_tracks.launch()
    except Exception:
        traceback.print_exc()
    finally:
        bpy.app.driver_namespace.pop(_TRACKS_TIMER_KEY, None)
    return None


def reload_retarget_ui() -> None:
    """Reload only the isolated Retarget panel and its modules in dependency order."""
    global animation_utils, blender_adapter, blender_artifacts, blender_debug, blender_debug_compare, blender_source_adapter, blender_ui, mapping, workflow

    blender_ui.unregister()
    mapping = importlib.reload(mapping)
    workflow = importlib.reload(workflow)
    blender_adapter = importlib.reload(blender_adapter)
    blender_source_adapter = importlib.reload(blender_source_adapter)
    blender_debug = importlib.reload(blender_debug)
    blender_debug_compare = importlib.reload(blender_debug_compare)
    animation_utils = importlib.reload(animation_utils)
    blender_artifacts = importlib.reload(blender_artifacts)
    blender_ui = importlib.reload(blender_ui)
    blender_ui.register()


def register():
    # The Retarget panel does not access context during registration. Register
    # it synchronously so a failure in the unrelated timeline UI cannot hide it.
    blender_ui.register()
    previous = bpy.app.driver_namespace.get(_TRACKS_TIMER_KEY)
    if previous is not None and bpy.app.timers.is_registered(previous):
        bpy.app.timers.unregister(previous)
    bpy.app.driver_namespace[_TRACKS_TIMER_KEY] = _launch_timeline_tracks_after_registration
    bpy.app.timers.register(_launch_timeline_tracks_after_registration, first_interval=0.1)


def unregister():
    pending = bpy.app.driver_namespace.pop(_TRACKS_TIMER_KEY, None)
    if pending is not None and bpy.app.timers.is_registered(pending):
        bpy.app.timers.unregister(pending)
    blender_ui.unregister()
    ui_tracks.stop()
