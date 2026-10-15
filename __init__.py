"""Extension entry point and UI lifecycles."""

from __future__ import annotations

import traceback

import bpy

from .ui import ui_retargeting, ui_tracks
from .animation.retargeting import blender_retarget_watcher


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


def register():
    # The Retarget panel does not access context during registration. Register
    # it synchronously so a failure in the unrelated timeline UI cannot hide it.
    ui_retargeting.register()
    blender_retarget_watcher.register()
    blender_retarget_watcher.schedule_target_initialization()
    previous = bpy.app.driver_namespace.get(_TRACKS_TIMER_KEY)
    if previous is not None and bpy.app.timers.is_registered(previous):
        bpy.app.timers.unregister(previous)
    bpy.app.driver_namespace[_TRACKS_TIMER_KEY] = _launch_timeline_tracks_after_registration
    bpy.app.timers.register(_launch_timeline_tracks_after_registration, first_interval=0.1)


def unregister():
    pending = bpy.app.driver_namespace.pop(_TRACKS_TIMER_KEY, None)
    if pending is not None and bpy.app.timers.is_registered(pending):
        bpy.app.timers.unregister(pending)
    blender_retarget_watcher.unregister()
    ui_retargeting.unregister()
    ui_tracks.stop()
