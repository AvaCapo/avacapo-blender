import requests

from .config import Config
from .logger import log
from .storage import Storage

_model_names_cache: list[str] | None = None


def get_models_names() -> list[str]:
    global _model_names_cache

    if _model_names_cache is None:
        payload = {"api_token": Storage.api_token}

        try:
            response = requests.get(Config.GET_MODELS_NAMES_URL, params=payload)
            if response.status_code == 200:
                models = response.json().get("models", [])
                _model_names_cache = models or Config.DEFAULT_MODELS
            else:
                log.error(f"Failed to get models names: {response.status_code} {response.text}")
                _model_names_cache = Config.DEFAULT_MODELS
        except Exception as exc:
            log.error(f"Failed to get models names: {exc}")
            _model_names_cache = Config.DEFAULT_MODELS

    return _model_names_cache
