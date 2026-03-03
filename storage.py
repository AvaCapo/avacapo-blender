import os
import json
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger('blender_logger')

STORAGE_FILE_NAME = "storage.json"
current_dir = os.getcwd()
#? current_dir = os.path.dirname(os.path.abspath(__file__))
STORAGE_PATH = os.path.join(current_dir, STORAGE_FILE_NAME)

# Serialization / deser.. via dataclass, asdict, **

@dataclass
class Storage:
    api_token: str = ""

def _load() -> Storage:
    try:
        with open(STORAGE_PATH) as f:
            return Storage(**json.load(f))
    except FileNotFoundError:
        logger.warning("Storage file not found, creating.")
    except Exception as e:
        logger.error("Storage unreadable (%s), recreating.", e)

    return _save(Storage())

def _save(storage: Storage) -> Storage:
    with open(STORAGE_PATH, "w") as f:
        json.dump(asdict(storage), f, indent=2)
    return storage

def get_api_token() -> str | None:
    return _load().api_token or None

def set_api_token(token: str) -> None:
    storage = _load()
    storage.api_token = token
    _save(storage)
