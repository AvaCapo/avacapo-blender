"""Generation-model choices for the Blender UI."""

import requests

from ..common.config import Config
from ..common.logger import log


_fallback_model_enum_items = tuple(
    (
        model_type,
        Config.DEFAULT_MODEL_DETAILS.get(model_type, {}).get("name", model_type),
        Config.DEFAULT_MODEL_DETAILS.get(model_type, {}).get("description", model_type),
    )
    for model_type in Config.DEFAULT_MODELS
)
_model_enum_items = _fallback_model_enum_items


def reset_model_enum_items() -> None:
    """Restore the configured fallback after login, logout, or a failed request."""
    global _model_enum_items
    _model_enum_items = _fallback_model_enum_items


def _items_from_response(payload: dict) -> tuple[tuple[str, str, str], ...]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return _fallback_model_enum_items

    items = []
    known_types = set()
    for model in raw_models:
        if not isinstance(model, dict):
            continue
        model_type = str(model.get("type", "")).strip()
        if not model_type or model_type in known_types:
            continue
        known_types.add(model_type)
        items.append((
            model_type,
            str(model.get("name") or model_type),
            str(model.get("description") or ""),
        ))
    return tuple(items) or _fallback_model_enum_items


def fetch_model_enum_items(api_token: str) -> tuple[tuple[str, str, str], ...]:
    """Fetch server models outside Blender's draw callback."""
    if not api_token:
        return _fallback_model_enum_items

    log.info("Fetching model catalog from server...")
    try:
        response = requests.get(
            Config.GET_MODELS_URL,
            params={"api_token": api_token},
            timeout=10,
        )
        if response.status_code == 200:
            return _items_from_response(response.json())
        log.error(f"Failed to get models: {response.status_code} {response.text}")
    except Exception as exc:
        log.error(f"Failed to get models: {exc}")
    return _fallback_model_enum_items


def set_model_enum_items(items: tuple[tuple[str, str, str], ...]) -> None:
    """Publish the last fetched model list for Blender EnumProperty callbacks."""
    global _model_enum_items
    _model_enum_items = items or _fallback_model_enum_items


def get_model_enum_items(_self, _context) -> tuple[tuple[str, str, str], ...]:
    """Return cached items only; this callback must never do network I/O."""
    return _model_enum_items
