"""Take operations on records supplied by a storage adapter; no Blender API."""

import json

from ..common.config import Config
from .generation_models import (
    ConstraintsParameters, GenerationCommand, InbetweenParameters, TextParameters,
)
from .schemas import TakeData


def current_take(clip):
    return find_take(clip, clip.active_take_uid) if clip.active_take_uid else None


def find_take(clip, take_uid: int):
    return next((take for take in clip.takes if take.uid == take_uid), None)


def add_take(
    store, clip, *, command: GenerationCommand | None = None,
    uid: int | None = None, activate: bool = True,
):
    """Allocate a Take owned by ``clip`` and optionally select it."""
    if uid is None:
        clip.take_counter += 1
        uid = clip.take_counter
    elif uid <= 0 or find_take(clip, uid) is not None:
        raise ValueError("A Take UID must be positive and unique within its Clip.")
    else:
        clip.take_counter = max(clip.take_counter, uid)
    take = store.allocate_take(clip)
    take.uid = uid
    take.name = f"Take {len(clip.takes)}"
    take.length = clip.end - clip.start
    _set_generation_request(take, command or GenerationCommand(
        parameters=TextParameters(duration=(clip.end - clip.start) / Config.DEFAULT_FPS),
    ))
    take.offset = 0
    take.progress = 0.0
    if activate:
        clip.active_take_uid = take.uid
    return take


def select_take(clip, take_uid: int) -> bool:
    if find_take(clip, take_uid) is None:
        return False
    clip.active_take_uid = take_uid
    return True


def delete_takes(store, clip) -> None:
    store.clear_take_records(clip)
    clip.active_take_uid = 0
    clip.prompt = ""
    clip.seed = -1
    clip.generation_model = Config.DEFAULT_MODEL_TYPE
    clip.generation_temperature = Config.DEFAULT_TEMPERATURE
    clip.generation_cycled = False
    clip.generation_in_place = False


def remove_take(store, clip, take_uid: int) -> bool:
    """Remove one Take record without touching resources owned by its caller."""
    if find_take(clip, take_uid) is None:
        return False
    store.remove_take_record(clip, take_uid)
    if clip.active_take_uid == take_uid:
        clip.active_take_uid = 0
    return True


def shift_offsets(clip, start_delta: int) -> None:
    """Keep takes fixed in timeline time when the clip's left border changes."""
    for take in clip.takes:
        take.offset -= start_delta


def restore_take(store, clip, payload: TakeData):
    take = add_take(store, clip, uid=payload.uid)
    take.name = payload.name
    for field in (
        "prompt", "generation_mode", "generation_name", "generation_model",
        "generation_in_place", "duration_seconds", "fps", "temperature", "cycled",
        "seed", "left_frame", "right_frame", "left_context_frames",
        "right_context_frames", "gap_frames", "end_offset_x", "end_offset_z",
        "align_heading", "length", "constraint_frames",
    ):
        setattr(take, field, getattr(payload, field))
    take.offset = int(payload.offset)
    take.progress = min(1.0, max(0.0, payload.progress))
    return take


def _set_generation_request(take, command: GenerationCommand) -> None:
    """Store scalar request inputs; binary context files are intentionally excluded."""
    if not isinstance(command, GenerationCommand):
        raise ValueError("A GenerationCommand is required.")
    command = command.resolve_seed()
    take.prompt = command.prompt
    take.generation_mode = command.mode.value
    take.generation_name = command.name
    take.generation_model = command.model
    take.generation_in_place = command.in_place
    if isinstance(command.parameters, TextParameters):
        parameters = command.parameters
        take.duration_seconds = parameters.duration
        take.fps = parameters.fps
        take.temperature = parameters.temperature
        take.cycled = parameters.cycled
        take.seed = parameters.seed
        return
    if isinstance(command.parameters, InbetweenParameters):
        parameters = command.parameters
        take.seed = parameters.seed
        take.left_frame = parameters.left_frame
        take.right_frame = parameters.right_frame
        take.left_context_frames = parameters.left_context_frames
        take.right_context_frames = parameters.right_context_frames
        take.gap_frames = parameters.gap_frames
        take.end_offset_x = parameters.end_offset_x
        take.end_offset_z = parameters.end_offset_z
        take.align_heading = parameters.align_heading
        return
    if isinstance(command.parameters, ConstraintsParameters):
        parameters = command.parameters
        take.duration_seconds = parameters.num_frames / Config.DEFAULT_FPS
        take.fps = Config.DEFAULT_FPS
        take.seed = parameters.seed
        take.cycled = parameters.cycled
        take.constraint_frames = json.dumps([item.target_frame for item in parameters.constraints])
        return
    raise ValueError("Unsupported generation command.")
