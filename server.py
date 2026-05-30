import bpy
import requests

from .storage import Storage
from .logger import log
from .config import Config
from .models import get_default_model_type, get_models_names

config = Config()


def get_fps() -> int:
    """get fps from blender please"""
    try:
        return bpy.context.scene.render.fps
    except:
        return config.DEFAULT_FPS


def _build_animation_name(prompt: str) -> str:
    normalized_prompt = "_".join(prompt.split())[:15]
    return f"blender_{normalized_prompt or 'animation'}"


def get_animation(
    name: str | None = None,
    prompt: str = "",
    duration: float = 5.0,
    temperature: float = 1.0,
    model: str = "",
    in_place: bool = False,
):
    """send prompt to server, get animation back"""
    model_names = get_models_names()
    if not model:
        model = get_default_model_type()
    if model not in model_names:
        message = f"Invalid model type: {model}. Must be one of {model_names}."
        log.error(message)
        raise ValueError(message)

    payload = {
        "prompt": prompt,
        "prompt_duration": duration,
        "prompt_fps": get_fps(),
        "prompt_temperature": temperature,
        "name": name or _build_animation_name(prompt),
        "api_token": Storage.api_token,
        "model": model,
        "extension": "bvh",
        "include_skin": False,
        "in_place": in_place,
    }
    log.info(f"Sending request with payload: {payload}")

    try:
        response = requests.post(config.GENERATION_URL, json=payload, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.error(f"Animation request failed: {exc}")
        raise RuntimeError("Failed to fetch animation from server.") from exc

    return response.content
