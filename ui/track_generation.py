"""Blender generation adapter. Currently completes a take using a demo timer."""

import bpy

from ..core import takes, tracks
from .track_runtime import tag_redraw


TIMER_CALLBACKS = set()
_DEMO_DELAY = 0.5


def request_generation(scene, clip_uid: int, take_uid: int) -> None:
    """Demo backend: complete the take after half a second."""
    def tick():
        TIMER_CALLBACKS.discard(tick)
        try:
            # A live scene reference survives renaming; deletion/load invalidates it.
            if bpy.data.scenes.get(scene.name) != scene:
                return None
            data = getattr(scene, "clip_data", None)
            clip = tracks.find_clip(data.clips, clip_uid) if data is not None else None
            take = takes.find_take(clip, take_uid) if clip is not None else None
            if take is not None:
                take.progress = 1.0
                tag_redraw()
        except ReferenceError:
            pass
        return None

    bpy.app.timers.register(tick, first_interval=_DEMO_DELAY)
    TIMER_CALLBACKS.add(tick)


def cancel_generation(callbacks=None) -> None:
    callbacks = TIMER_CALLBACKS if callbacks is None else callbacks
    for callback in tuple(callbacks):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    callbacks.clear()
