import os
from typing import Literal

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))

BLEND_NAME = "rigs.blend"
BLEND_PATH = os.path.join(ADDON_DIR, BLEND_NAME)

STORAGE = "storage.json"
STORAGE_PATH = os.path.join(ADDON_DIR, STORAGE)

AVACAPO_RIG_NAME = "avacapo_bvh_v1"

model_types = ["asm", "gen1", "gen2"]
modelType = Literal[*model_types]
