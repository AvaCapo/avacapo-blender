import os
import json
from .logger import log
from .config import STORAGE_PATH


class Storage:
    api_token: str = ""
    base_url: str = ""

    @classmethod
    def load(cls) -> None:
        log.debug("storage loading")
        try:
            with open(STORAGE_PATH) as f:
                _dict = json.load(f)
            for k, v in _dict.items():
                if k in cls.__annotations__:
                    setattr(cls, k, v)
                else:
                    log.warning("Unknown key in storage: '%s', skipping.", k)
        except FileNotFoundError:
            log.warning("Storage file not found, creating.")
            cls.save()
        except json.JSONDecodeError as e:
            log.error("Storage file corrupt: %s", e)

    @classmethod
    def save(cls) -> None:
        known_fields = cls.__annotations__
        data = {}
        for key in known_fields:
            data[key] = getattr(cls, key)
        with open(STORAGE_PATH, "w") as f:
            json.dump(data, f, indent=2)


Storage.load()
log.debug(Storage.api_token)
