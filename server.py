# Module to exchange info with server
import os
import requests
from typing import Literal
import bpy

from .storage import get_api_token


URL = "http://185.70.185.83:8888/api/v1/api-avacapo-prompt/"
type modelType = Literal["asm", "gen1", "gen2"]


def get_fps() -> int:
    """ get fps from blender please """
    return 24


def get_animation(name, prompt: str, duration: float, temperature: float, model: modelType):
    """ send prompt to server, get animation back """
    fps = get_fps()
    api_token = get_api_token()
    ext = 'bvh'
    payload = {
        "prompt": prompt,
        "prompt_duration": duration,
        "prompt_fps": fps,
        "prompt_temperature": 1,
        # why do we even need a name????
        "name": name,
        "api_token": api_token,
        "model": model,
        "extension": ext,
    }

    response = requests.post(URL, json=payload)
    print("Status:", response.status_code)
    result = ''
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if chunk:
            result += chunk

    return result
