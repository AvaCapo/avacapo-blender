import json
import os
from pathlib import Path
import tempfile

from ..common.logger import log
from ..common.config import Config

config = Config()


class Storage:
    api_token: str = ""

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> bool:
        """Explicitly load settings; a missing file never creates one."""
        try:
            with open(path or config.STORAGE_PATH, encoding="utf-8") as f:
                _dict = json.load(f)
            if not isinstance(_dict, dict):
                raise ValueError("Settings must be a JSON object.")
            values = {key: _dict.get(key, "") for key in cls.__annotations__}
            if any(not isinstance(value, str) for value in values.values()):
                raise ValueError("Settings fields must be strings.")
        except FileNotFoundError:
            cls.api_token = ""
            return False
        except (OSError, ValueError):
            cls.api_token = ""
            log.error("Could not load settings: unreadable or invalid settings file.")
            return False
        for key, value in values.items():
            setattr(cls, key, value)
        return True

    @classmethod
    def save(cls, path: str | os.PathLike | None = None) -> None:
        """Replace the file atomically, without truncating existing settings on failure."""
        destination = Path(path or config.STORAGE_PATH)
        data = {key: getattr(cls, key) for key in cls.__annotations__}
        if any(not isinstance(value, str) for value in data.values()):
            raise ValueError("Settings fields must be strings.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=destination.parent, delete=False
            ) as stream:
                temporary = stream.name
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
