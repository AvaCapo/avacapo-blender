"""Extension entry point and UI lifecycles."""

from __future__ import annotations

import importlib
import traceback

import bpy

from .ui import ui_tracks
from .animation import animation_utils
from .animation.retargeting import (
    action_provenance,
    blender_adapter,
    blender_motion_writer,
    blender_retarget_service,
    blender_retarget_watcher,
    blender_source_adapter,
    blender_ui,
    canonical_source,
    known_rig_mapping,
    mapping,
    rest_hierarchy,
    workflow,
)
# DEBUG ONLY:
from .animation.retargeting.debug import (
    artifacts as retarget_debug_artifacts,
    root_height_capture as retarget_debug_root_height_capture,
    source_comparison as retarget_debug_source_comparison,
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
    global action_provenance, animation_utils, blender_adapter, blender_motion_writer, blender_retarget_service, blender_retarget_watcher, blender_source_adapter, blender_ui, canonical_source, known_rig_mapping, mapping, rest_hierarchy, retarget_debug_artifacts, retarget_debug_root_height_capture, retarget_debug_source_comparison, workflow

    blender_retarget_watcher.unregister()
    blender_ui.unregister()
    canonical_source = importlib.reload(canonical_source)
    mapping = importlib.reload(mapping)
    workflow = importlib.reload(workflow)
    rest_hierarchy = importlib.reload(rest_hierarchy)
    blender_adapter = importlib.reload(blender_adapter)
    blender_source_adapter = importlib.reload(blender_source_adapter)
    action_provenance = importlib.reload(action_provenance)
    known_rig_mapping = importlib.reload(known_rig_mapping)
    retarget_debug_source_comparison = importlib.reload(retarget_debug_source_comparison)
    retarget_debug_root_height_capture = importlib.reload(retarget_debug_root_height_capture)
    animation_utils = importlib.reload(animation_utils)
    blender_motion_writer = importlib.reload(blender_motion_writer)
    retarget_debug_artifacts = importlib.reload(retarget_debug_artifacts)
    blender_retarget_service = importlib.reload(blender_retarget_service)
    blender_retarget_watcher = importlib.reload(blender_retarget_watcher)
    blender_ui = importlib.reload(blender_ui)
    blender_ui.register()
    blender_retarget_watcher.register()
    blender_retarget_watcher.schedule_target_initialization()


def register():
    # The Retarget panel does not access context during registration. Register
    # it synchronously so a failure in the unrelated timeline UI cannot hide it.
    blender_ui.register()
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
    blender_ui.unregister()
    ui_tracks.stop()
