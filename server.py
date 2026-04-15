import bpy
import requests
from typing import Literal
import time

from .storage import Storage
from .logger import log
from .config import Config
from .models import get_models_names

config = Config()


def get_fps() -> int:
    """get fps from blender please"""
    return bpy.context.scene.render.fps


def get_animation(
    name: str = "test_animation",
    prompt: str = "",
    duration: float = 5.0,
    temperature: float = 1.0,
    model: str = "gen2",
):
    """send prompt to server, get animation back"""
    model_names = get_models_names()
    if model not in model_names:
        log.error(f"Invalid model type: {model}. Must be one of {model_names}.")
        return None
    
    time_start = time.time()
    payload = {
        "prompt": prompt,
        "prompt_duration": duration,
        "prompt_fps": get_fps(),
        "prompt_temperature": temperature,
        "name": name,
        "api_token": Storage.api_token,
        "model": model,
        "extension": "bvh",
    }
    print(payload)
    response = requests.post(config.GENERATION_URL, json=payload)
    time_end = time.time()
    print("---")
    print(f"response getting took {time_end - time_start:.2f}s")
    print("---")
    return response.content
