import os
import json
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger('blender_logger')

STORAGE_FILE_NAME = "storage.json"
current_dir = os.getcwd()
STORAGE_PATH = os.path.join(current_dir, STORAGE_FILE_NAME)


# TODO: add logging and validation is sheme is off (use)
@dataclass
class Storage:
    api_token: str = ""

    def load(self) -> None:
        try:
            with open(STORAGE_PATH) as f:
                self.__dict__.update(json.load(f))
        except FileNotFoundError:
            logger.warning("Storage file not found, creating.")
            self.save()

    def save(self) -> None:
        with open(STORAGE_PATH, "w") as f:
            json.dump(asdict(self), f, indent=2)
