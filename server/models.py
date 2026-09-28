import requests

from .config import Config
from .logger import log
from .storage import Storage

_model_catalog_cache: list[dict[str, str]] | None = None
_model_enum_items_cache: list[tuple[str, str, str]] | None = None


def clear_models_cache() -> None:
    global _model_catalog_cache
    global _model_enum_items_cache

    _model_catalog_cache = None
    _model_enum_items_cache = None


def _build_default_model_entry(model_type: str) -> dict[str, str]:
    defaults = Config.DEFAULT_MODEL_DETAILS.get(model_type, {})
    return {
        "type": model_type,
        "name": defaults.get("name", model_type.capitalize()),
        "description": defaults.get("description", f"{model_type} model"),
    }


def _get_default_model_catalog(model_types: list[str] | None = None) -> list[dict[str, str]]:
    return [_build_default_model_entry(model_type) for model_type in (model_types or Config.DEFAULT_MODELS)]


def _prioritize_default_model(catalog: list[dict[str, str]]) -> list[dict[str, str]]:
    preferred_type = Config.DEFAULT_MODEL_TYPE
    preferred_index = next(
        (index for index, model in enumerate(catalog) if model.get("type") == preferred_type),
        None,
    )

    if preferred_index in (None, 0):
        return catalog

    preferred_model = catalog[preferred_index]
    return [preferred_model, *catalog[:preferred_index], *catalog[preferred_index + 1 :]]


def _normalize_model_catalog(payload: dict) -> list[dict[str, str]]:
    raw_model_types = payload.get("model_types", [])
    raw_models = payload.get("models", [])

    if isinstance(raw_models, list) and raw_models and all(isinstance(model, str) for model in raw_models):
        return _get_default_model_catalog([model for model in raw_models if model])

    models_by_type: dict[str, dict[str, str]] = {}
    if isinstance(raw_models, list):
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue

            model_type = str(raw_model.get("type", "")).strip()
            if not model_type:
                continue

            default_entry = _build_default_model_entry(model_type)
            models_by_type[model_type] = {
                "type": model_type,
                "name": str(raw_model.get("name") or default_entry["name"]),
                "description": str(raw_model.get("description") or default_entry["description"]),
            }

    ordered_types: list[str] = []
    if isinstance(raw_model_types, list):
        for raw_type in raw_model_types:
            model_type = str(raw_type).strip()
            if model_type and model_type not in ordered_types:
                ordered_types.append(model_type)

    if not ordered_types:
        ordered_types = list(models_by_type)

    catalog: list[dict[str, str]] = []
    seen: set[str] = set()

    for model_type in ordered_types:
        catalog.append(models_by_type.get(model_type, _build_default_model_entry(model_type)))
        seen.add(model_type)

    for model_type, model_info in models_by_type.items():
        if model_type not in seen:
            catalog.append(model_info)

    return _prioritize_default_model(catalog or _get_default_model_catalog())


def get_model_catalog() -> list[dict[str, str]]:
    global _model_catalog_cache

    if _model_catalog_cache is not None:
        return _model_catalog_cache

    if not Storage.api_token:
        _model_catalog_cache = _prioritize_default_model(_get_default_model_catalog())
        return _model_catalog_cache

    payload = {"api_token": Storage.api_token}
    log.info("Fetching model catalog from server...")
    try:
        response = requests.get(Config.GET_MODELS_URL, params=payload, timeout=10)
        if response.status_code == 200:
            log.info("Model catalog fetched successfully, models: " + ", ".join(response.json().get("model_types", [])))
            _model_catalog_cache = _normalize_model_catalog(response.json())
        else:
            log.error(f"Failed to get models: {response.status_code} {response.text}")
            _model_catalog_cache = _prioritize_default_model(_get_default_model_catalog())
    except Exception as exc:
        log.error(f"Failed to get models: {exc}")
        _model_catalog_cache = _prioritize_default_model(_get_default_model_catalog())

    return _model_catalog_cache


def get_models_names() -> list[str]:
    return [model["type"] for model in get_model_catalog()]


def get_model_enum_items(_self, _context) -> list[tuple[str, str, str]]:
    global _model_enum_items_cache

    _model_enum_items_cache = [
        (model["type"], model["name"], model["description"]) for model in get_model_catalog()
    ]
    return _model_enum_items_cache


def get_default_model_type() -> str:
    catalog = get_model_catalog()
    if catalog:
        return catalog[0]["type"]
    return Config.DEFAULT_MODELS[0]


def resolve_model_type(model_type: str) -> str:
    model_names = get_models_names()
    if model_type and model_type in model_names:
        return model_type
    return get_default_model_type()
