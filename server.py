# Module to exchange info with server
from storage import Storage
import os
import requests
from typing import Literal
import logging
import bpy
logger = logging.getLogger('blender_logger')

storage = Storage()


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
        "api_token": storage.api_token,
        "model": model,
        "extension": ext,
    }

    response = requests.post(URL, json=payload)
    logger.debug("Status:", response.status_code)
    result = b''
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        print(chunk)
        if chunk:
            result += chunk
    return result
