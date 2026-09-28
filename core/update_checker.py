import bpy
import threading

import requests

from .config import Config
from .logger import log
from .state_controller import State
from .storage import Storage
from .version import ADDON_VERSION, is_version_less, normalize_version

config = Config()


def _tag_ui_redraw() -> None:
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return

    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _refresh_update_notice_timer() -> float | None:
    if State.update_check_started:
        return 0.5

    _tag_ui_redraw()
    return None


def _parse_version(value) -> tuple[int, ...] | None:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(".") if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None

    if not parts:
        return None

    normalized_parts: list[int] = []
    for part in parts:
        if isinstance(part, bool):
            return None
        if isinstance(part, int):
            normalized_parts.append(part)
            continue
        if isinstance(part, float) and part.is_integer():
            normalized_parts.append(int(part))
            continue
        if isinstance(part, str) and part.isdigit():
            normalized_parts.append(int(part))
            continue
        return None

    return normalize_version(tuple(normalized_parts))


def _check_for_updates_worker() -> None:
    try:
        response = requests.get(
            config.GET_ADDON_VERSION_URL,
            params={"api_token": Storage.api_token},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        latest_version = _parse_version(payload.get("version"))
        if latest_version is None:
            raise ValueError(f"Invalid addon version payload: {payload!r}")

        State.latest_addon_version = latest_version
        State.update_available = is_version_less(State.current_addon_version, latest_version)
    except Exception as exc:
        State.latest_addon_version = ()
        State.update_available = False
        log.warning("Addon update check failed: %s", exc)
    finally:
        State.update_check_done = True
        State.update_check_started = False


def reset_update_check_state() -> None:
    State.current_addon_version = normalize_version(ADDON_VERSION)
    State.latest_addon_version = ()
    State.update_available = False
    State.update_check_done = False
    State.update_check_started = False


def start_update_check(*, force: bool = False) -> None:
    if State.update_check_started:
        return
    if State.update_check_done and not force:
        return

    reset_update_check_state()
    if not Storage.api_token:
        return

    State.update_check_started = True
    bpy.app.timers.register(_refresh_update_notice_timer, first_interval=0.5)

    thread = threading.Thread(
        target=_check_for_updates_worker,
        name="avacapo-update-check",
        daemon=True,
    )
    thread.start()
