"""Background refresh of generation-model choices shown by the UI."""

import threading

import bpy

from ..core.storage import Storage
from ..server import models


MODEL_CATALOG_TIMER_CALLBACKS = set()
_model_enum_items_thread = None
_model_enum_items_result = None
_model_enum_items_token = ""


def _tag_redraw() -> None:
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR'}:
            area.tag_redraw()


def _finish_model_enum_items_refresh() -> float | None:
    global _model_enum_items_result

    if _model_enum_items_thread is not None and _model_enum_items_thread.is_alive():
        return 0.1
    if _model_enum_items_result is not None and _model_enum_items_token == Storage.api_token:
        models.set_model_enum_items(_model_enum_items_result)
    _model_enum_items_result = None
    MODEL_CATALOG_TIMER_CALLBACKS.discard(_finish_model_enum_items_refresh)
    _tag_redraw()
    return None


def refresh_model_catalog() -> None:
    """Refresh server models without blocking Blender's UI thread."""
    global _model_enum_items_thread
    global _model_enum_items_result
    global _model_enum_items_token

    if not Storage.api_token or (
        _model_enum_items_thread is not None and _model_enum_items_thread.is_alive()
    ):
        return

    _model_enum_items_result = None
    _model_enum_items_token = Storage.api_token

    def fetch():
        global _model_enum_items_result
        _model_enum_items_result = models.fetch_model_enum_items(_model_enum_items_token)

    _model_enum_items_thread = threading.Thread(
        target=fetch, name="avacapo-model-catalog", daemon=True,
    )
    _model_enum_items_thread.start()
    if not bpy.app.timers.is_registered(_finish_model_enum_items_refresh):
        bpy.app.timers.register(_finish_model_enum_items_refresh, first_interval=0.1)
        MODEL_CATALOG_TIMER_CALLBACKS.add(_finish_model_enum_items_refresh)


def cancel_model_catalog_refresh(callbacks=None) -> None:
    callbacks = MODEL_CATALOG_TIMER_CALLBACKS if callbacks is None else callbacks
    for callback in tuple(callbacks):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    callbacks.clear()
