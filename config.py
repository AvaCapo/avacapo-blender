import os

class Config:
    """Configuration for the Avacapo addon."""
    ADDON_NAME = "avacapo"
    ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
    PLATFORM_URL = "https://service.avacapo.com/"
    FRONTEND_URL = "https://app.avacapo.com/"
    GENERATION_URL = f"{PLATFORM_URL}api/v1/api-avacapo-prompt/"
    GET_MODELS_NAMES_URL = f"{PLATFORM_URL}api/v1/get-text-model-types/"

    BLEND_NAME = "rigs.blend"
    BLEND_PATH = os.path.join(ADDON_DIR, BLEND_NAME)

    STORAGE = "storage.json"
    STORAGE_PATH = os.path.join(ADDON_DIR, STORAGE)

    AVACAPO_RIG_NAME = "avacapo_bvh_v1"

    DEFAULT_MODELS = ["asm", "gen1", "gen2"]
