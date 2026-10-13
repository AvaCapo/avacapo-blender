"""Take operations on records supplied by a storage adapter; no Blender API."""

from ..common.config import TimelineConfig
from .schemas import TakeData


def current_take(clip):
    if not clip.takes:
        return None
    index = min(max(clip.active_take, 0), len(clip.takes) - 1)
    return clip.takes[index]


def find_take(clip, uid: int):
    return next((take for take in clip.takes if take.uid == uid), None)


def add_take(store, clip, prompt: str | None = None):
    """Allocate a take, inherit clip settings and select the new record."""
    clip.take_counter += 1
    take = store.allocate_take(clip)
    take.uid = clip.take_counter
    take.name = f"Take {len(clip.takes)}"
    take.prompt = clip.prompt if prompt is None else prompt
    take.seed = clip.seed
    take.offset = 0
    take.length = max(TimelineConfig.MIN_TAKE_LENGTH, clip.end - clip.start)
    take.progress = 0.0
    clip.active_take = len(clip.takes) - 1
    return take


def select_take(clip, index: int) -> bool:
    if not 0 <= index < len(clip.takes):
        return False
    clip.active_take = index
    return True


def delete_takes(store, clip) -> None:
    store.clear_take_records(clip)
    clip.active_take = 0
    # Keep the counter: outstanding generation requests must not reuse a UID.


def shift_offsets(clip, start_delta: int) -> None:
    """Keep takes fixed in timeline time when the clip's left border changes."""
    for take in clip.takes:
        take.offset -= start_delta


def restore_take(store, clip, payload: TakeData):
    take = add_take(store, clip, prompt=payload.prompt)
    take.name = payload.name
    take.seed = payload.seed
    take.offset = payload.offset
    take.length = payload.length
    take.progress = min(1.0, max(0.0, payload.progress))
    return take
