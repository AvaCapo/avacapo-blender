# Module to exchange info with server
import os
import requests
from typing import Literal
import bpy

from .storage import get_api_token


URL = "http://185.70.185.83:8888/api/v1/api-avacapo-prompt/"
modelType = Literal["asm", "gen1", "gen2"]


def get_fps() -> int:
    """ get fps from blender please """
    return 24


def get_animation(
    name: str = "animation",
    prompt: str = "",
    duration: float = 5.0,
    temperature: float = 0.5,
    model: modelType = "asm"
):
    """ send prompt to server, get animation back """
    ext = 'bvh'
    payload = {
        "prompt": prompt,
        "prompt_duration": duration,
        "prompt_fps": 30,
        "prompt_temperature": temperature,
        "name": name,
        "api_token": "5c89cce212dd41fdb73afe462379a34c",
        "model": model,
        "extension": ext,
    }

    print("--------")
    print(payload)
    print("---------")
    response = requests.post(URL, json=payload)
    print("Status:", response.status_code)
    result = b''
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        print(chunk)
        if chunk:
            result += chunk
    return result
