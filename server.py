import bpy
import requests
from typing import Literal
import time

from .storage import Storage
from .logger import log

URL = "http://185.70.185.83:8888/api/v1/api-avacapo-prompt/"
modelType = Literal["asm", "gen1", "gen2"]


def get_fps() -> int:
    """get fps from blender please"""
    return bpy.context.scene.render.fps


def get_animation(
    name: str = "test_animation",
    prompt: str = "",
    duration: float = 5.0,
    temperature: float = 1.0,
    model: modelType = "asm",
):
    """send prompt to server, get animation back"""

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
    response = requests.post(URL, json=payload)
    time_end = time.time()
    print("---")
    print(f"response getting is {time_start - time_end}")
    print("---")
    return response.content
