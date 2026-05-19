import os


class Config:
    """Configuration for the Avacapo addon."""

    ADDON_NAME = "avacapo"
    ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
    PLATFORM_URL = "https://service.avacapo.com/"
    FRONTEND_URL = "https://app.avacapo.com/"
    GENERATION_URL = f"{PLATFORM_URL}api/v1/api-avacapo-prompt/"
    GET_MODELS_URL = f"{PLATFORM_URL}api/v1/get-text-model-types/"
    GET_MODELS_NAMES_URL = GET_MODELS_URL

    BLEND_NAME = "rigs.blend"
    BLEND_PATH = os.path.join(ADDON_DIR, BLEND_NAME)
    RIGS_DIR = os.path.join(ADDON_DIR, "rigs")
    MIXAMO_BVH_NAME = "mixamo.bvh"
    MIXAMO_BVH_PATH = os.path.join(RIGS_DIR, MIXAMO_BVH_NAME)

    STORAGE = "storage.json"
    STORAGE_PATH = os.path.join(ADDON_DIR, STORAGE)

    AVACAPO_RIG_NAME = "avacapo_bvh_v1"

    DEFAULT_MODELS = ["asm", "gen1", "gen2"]
    DEFAULT_MODEL_DETAILS = {
        "gen1": {
            "name": "Standard",
            "description": "Simple motion generation for quick results.",
        },
        "gen2": {
            "name": "Advanced",
            "description": "Fast, high-quality motion generation.",
        },
        "asm": {
            "name": "Constructor",
            "description": "Motion construction from a high-quality animation database.",
        },
    }
    DEFAULT_TEMPERATURE = 1.0
    DEFAULT_DURATION = 5
    DEFAULT_FPS = 24
