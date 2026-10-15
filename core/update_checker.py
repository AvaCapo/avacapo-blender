"""Check addon versions without accessing Blender or registering UI callbacks."""

import threading

import requests

from ..common.config import Config
from ..common.logger import log
from ..common.version import ADDON_VERSION, is_version_less, normalize_version
from .storage import Storage

config = Config()


class UpdateState:
    """Version-check progress and results available to callers."""

    current_addon_version: tuple[int, ...] = ()
    latest_addon_version: tuple[int, ...] = ()
    update_available: bool = False
    update_check_started: bool = False
    update_check_done: bool = False


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

        UpdateState.latest_addon_version = latest_version
        UpdateState.update_available = is_version_less(
            UpdateState.current_addon_version, latest_version
        )
    except Exception as exc:
        UpdateState.latest_addon_version = ()
        UpdateState.update_available = False
        log.warning("Addon update check failed: %s", exc)
    finally:
        UpdateState.update_check_done = True
        UpdateState.update_check_started = False


def reset_update_check_state() -> None:
    UpdateState.current_addon_version = normalize_version(ADDON_VERSION)
    UpdateState.latest_addon_version = ()
    UpdateState.update_available = False
    UpdateState.update_check_done = False
    UpdateState.update_check_started = False


def start_update_check(*, force: bool = False) -> None:
    """Start a background check; callers own any UI notification or polling."""
    if UpdateState.update_check_started:
        return
    if UpdateState.update_check_done and not force:
        return

    reset_update_check_state()
    if not Storage.api_token:
        return

    UpdateState.update_check_started = True
    thread = threading.Thread(
        target=_check_for_updates_worker,
        name="avacapo-update-check",
        daemon=True,
    )
    thread.start()
