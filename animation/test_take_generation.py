"""Temporary production POST adapter used until server jobs are available."""

import threading
from pathlib import Path

import bpy
import requests

from ..common.config import Config
from ..common.logger import log
from ..core.storage import Storage
from .retargeting import workflow


# TODO(test-generation): replace this bundled BVH adapter with the server result
# pipeline. The server result must still be written into a local-time Take Action.
_TEST_BVH_PATH = Path(__file__).resolve().parents[1] / "assets" / "tmp_animations" / "blender_backflip.bvh"


def load_test_motion():
    """Load the local fixture through Broom's established BVH policy."""
    source_motion = workflow.load_bvh_motion(_TEST_BVH_PATH)
    return source_motion, _TEST_BVH_PATH.read_text(encoding="utf-8")


_thread = None
_result = None
_error = None
_pending = None


def start_direct_generation(context, track, clip, command) -> None:
    """Generate one text Take through production's POST -> BVH response."""
    global _thread
    global _result
    global _error
    global _pending

    if not Storage.api_token:
        raise ValueError("Connect to AvaCapo before generating a Take.")
    if _thread is not None and _thread.is_alive():
        raise RuntimeError("Generation is already in progress.")

    command = command.resolve_seed()
    parameters = command.parameters
    api_token = Storage.api_token
    _pending = (track, clip, command, _scene_fps(context))
    _result = None
    _error = None

    payload = {
        "prompt": command.prompt,
        "prompt_duration": parameters.duration,
        "prompt_fps": parameters.fps,
        "prompt_temperature": parameters.temperature,
        "name": command.name or f"blender_{'_'.join(command.prompt.split())[:15] or 'animation'}",
        "api_token": api_token,
        "model": command.model,
        "extension": "bvh",
        "include_skin": False,
        "in_place": command.in_place,
        "cycled": parameters.cycled,
        "seed": parameters.seed,
    }

    def fetch():
        global _result
        global _error
        try:
            with requests.post(
                Config.GENERATION_URL,
                json=payload,
                timeout=Config.REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                _result = response.content
            if not _result:
                raise RuntimeError("Production server returned an empty animation.")
        except Exception as exc:
            _error = str(exc)
            log.exception("Temporary production generation failed")

    log.info("Starting temporary production POST generation for Clip %s.", clip.uid)
    _thread = threading.Thread(target=fetch, name="avacapo-direct-generation", daemon=True)
    _thread.start()
    if not bpy.app.timers.is_registered(_finish_direct_generation):
        bpy.app.timers.register(_finish_direct_generation, first_interval=0.1)


def _finish_direct_generation() -> float | None:
    global _pending
    global _result
    global _error

    if _thread is not None and _thread.is_alive():
        return 0.1

    pending, result, error = _pending, _result, _error
    _pending = _result = _error = None
    if pending is None:
        return None
    if error is not None:
        log.error("Temporary production generation failed: %s", error)
        return None
    if not result:
        log.error("Temporary production generation returned no BVH.")
        return None

    try:
        from broom.io import load_bvh_from_bytes

        from ..timeline import operations
        from ..timeline.runtime import tag_redraw

        track, clip, command, scene_fps = pending
        operations.apply_generated_motion(
            track,
            clip,
            command=command,
            source_motion=load_bvh_from_bytes(result),
            source_text=result.decode("utf-8"),
            scene_fps=scene_fps,
        )
        tag_redraw()
        log.info("Temporary production generation finished.")
    except Exception:
        log.exception("Could not create Take from temporary production result")
    return None


def cancel_direct_generation() -> None:
    """Stop observing a request while Blender unloads; requests itself cannot cancel POST."""
    global _pending

    if bpy.app.timers.is_registered(_finish_direct_generation):
        bpy.app.timers.unregister(_finish_direct_generation)
    _pending = None


def _scene_fps(context) -> float:
    return float(context.scene.render.fps) / float(context.scene.render.fps_base)
