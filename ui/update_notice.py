"""Blender timer and redraw adapter for the core version checker."""

import bpy


TIMER_CALLBACKS = set()


def _tag_ui_redraw() -> None:
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return

    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _refresh_update_notice_timer() -> float | None:
    from ..core.update_checker import UpdateState

    if UpdateState.update_check_started:
        return 0.5

    TIMER_CALLBACKS.discard(_refresh_update_notice_timer)
    _tag_ui_redraw()
    return None


def start_update_notice(*, force: bool = False) -> None:
    """Start a check and watch its result; call from Blender's main thread."""
    # Keep HTTP dependencies optional until the UI requests a version check.
    from ..core import update_checker

    update_checker.start_update_check(force=force)
    state = update_checker.UpdateState
    if not state.update_check_started and not state.update_check_done:
        return
    if not bpy.app.timers.is_registered(_refresh_update_notice_timer):
        bpy.app.timers.register(_refresh_update_notice_timer, first_interval=0.5)
        TIMER_CALLBACKS.add(_refresh_update_notice_timer)


def stop_update_notice(callbacks=None) -> None:
    """Remove redraw timers on addon unload or file loading."""
    callbacks = TIMER_CALLBACKS if callbacks is None else callbacks
    for callback in tuple(callbacks):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    callbacks.clear()
