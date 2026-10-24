"""Timeline rules operating on records and a minimal allocation adapter.

Mutations expect raw record writes. A Blender adapter suppresses RNA callbacks
for the duration of each call; ordinary Python records need no such guard.
"""

import math

from ..common.config import TimelineConfig
from . import takes
from .schemas import ClipData, ClipSnapshot, DragSession, TakeData


def ordered_clips(clips) -> list:
    return sorted(clips, key=lambda clip: (clip.start, clip.end))


def find_clip(clips, uid: int):
    return next((clip for clip in clips if clip.uid == uid), None)


def active_clip(data):
    return find_clip(data.clips, data.active_clip_uid) if data.active_clip_uid else None


def neighbors(clips, uid: int) -> tuple:
    ordered = ordered_clips(clips)
    for index, clip in enumerate(ordered):
        if clip.uid == uid:
            previous = ordered[index - 1] if index else None
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            return previous, following
    return None, None


def is_free(clips, start: int, end: int, ignore_uid: int = 0) -> bool:
    return all(
        not (start < clip.end and end > clip.start)
        for clip in clips if clip.uid != ignore_uid
    )


def gap_at(clips, frame: int) -> tuple | None:
    left, right = None, None
    for clip in clips:
        if clip.start <= frame < clip.end:
            return None
        if clip.end <= frame:
            left = clip.end if left is None else max(left, clip.end)
        if clip.start > frame:
            right = clip.start if right is None else min(right, clip.start)
    return left, right


def new_range(clips, frame: float, default_length: int) -> tuple[int, int] | None:
    frame = math.floor(frame)
    gap = gap_at(clips, frame)
    if gap is None:
        return None
    end = frame + max(TimelineConfig.MIN_CLIP_LENGTH, default_length)
    if gap[1] is not None:
        end = min(end, gap[1])
    return (frame, end) if end > frame else None


def fill_ranges(
    clips, frame: float, default_length: int, scene_start: int, scene_end: int,
) -> list[tuple[int, int]]:
    frame = math.floor(frame)
    gap = gap_at(clips, frame)
    if gap is None:
        return []
    left, right = gap
    default_length = max(TimelineConfig.MIN_CLIP_LENGTH, default_length)
    start = left if left is not None else min(scene_start, frame)
    end = right if right is not None else max(scene_end + 1, start + default_length)
    if end <= frame:
        start, end = frame, frame + default_length
    total = end - start
    count = max(1, min(total, round(total / default_length)))
    base_length, remainder = divmod(total, count)
    ranges = []
    cursor = start
    for index in range(count):
        range_end = cursor + base_length + (1 if index < remainder else 0)
        ranges.append((cursor, range_end))
        cursor = range_end
    return ranges


def left_limit(clips, uid: int) -> int | None:
    previous, _ = neighbors(clips, uid)
    if previous is None:
        return None
    return (
        previous.start + TimelineConfig.MIN_CLIP_LENGTH
        if previous.bind_end else previous.end
    )


def right_limit(clips, uid: int) -> int | None:
    _, following = neighbors(clips, uid)
    if following is None:
        return None
    return (
        following.end - TimelineConfig.MIN_CLIP_LENGTH
        if following.bind_start else following.start
    )


def _set_start_preserving_takes(clip, start: int) -> None:
    delta = start - clip.start
    clip.start = start
    takes.shift_offsets(clip, delta)


def apply_bindings(store) -> None:
    """Preserve the original bounded, ordered propagation of neighbour links."""
    for _ in range(len(store.clips) + 1):
        changed = False
        ordered = ordered_clips(store.clips)
        for index, clip in enumerate(ordered):
            if clip.bind_start and index > 0:
                previous = ordered[index - 1]
                if clip.start != previous.end and previous.end < clip.end:
                    _set_start_preserving_takes(clip, previous.end)
                    changed = True
            if clip.bind_end and index + 1 < len(ordered):
                following = ordered[index + 1]
                if clip.end != following.start and following.start > clip.start:
                    clip.end = following.start
                    changed = True
        if not changed:
            break


def snapshot(store) -> dict[int, ClipSnapshot]:
    return {
        clip.uid: ClipSnapshot(
            clip.start, clip.end, clip.bind_start, clip.bind_end,
            tuple((take.uid, take.offset) for take in clip.takes),
        )
        for clip in store.clips
    }


def restore_snapshot(store, initial: dict[int, ClipSnapshot]) -> None:
    """Restore raw state without applying bindings; match takes by UID."""
    for clip in store.clips:
        state = initial.get(clip.uid)
        if state is None:
            continue
        clip.start, clip.end = state.start, state.end
        clip.bind_start, clip.bind_end = state.bind_start, state.bind_end
        offsets = dict(state.take_offsets)
        for take in clip.takes:
            if take.uid in offsets:
                take.offset = offsets[take.uid]


def add_clip(store, start: int, end: int, name: str | None = None):
    if end - start < TimelineConfig.MIN_CLIP_LENGTH:
        raise ValueError("A clip must span at least one frame")
    clip = store.allocate_clip()
    clip.uid = store.data.next_clip_uid
    store.data.next_clip_uid += 1
    clip.name = name or f"Clip {clip.uid}"
    clip.start, clip.end = start, end
    return clip


def remove_clip(store, uid: int) -> bool:
    if find_clip(store.clips, uid) is None:
        return False
    previous, following = neighbors(store.clips, uid)
    if previous is not None and previous.bind_end:
        previous.bind_end = False
    if following is not None and following.bind_start:
        following.bind_start = False
    store.remove_clip_record(uid)
    if store.data.active_clip_uid == uid:
        store.data.active_clip_uid = 0
    return True


def serialize_clip(clip) -> ClipData:
    """Copy clip values without retaining references to Blender records."""
    return ClipData(
        name=clip.name,
        length=clip.end - clip.start,
        active_take_uid=clip.active_take_uid,
        takes=tuple(
            TakeData(
                uid=take.uid,
                name=take.name,
                prompt=take.prompt,
                generation_mode=take.generation_mode,
                generation_name=take.generation_name,
                generation_model=take.generation_model,
                generation_in_place=take.generation_in_place,
                duration_seconds=take.duration_seconds,
                fps=take.fps,
                temperature=take.temperature,
                cycled=take.cycled,
                seed=take.seed,
                left_frame=take.left_frame,
                right_frame=take.right_frame,
                left_context_frames=take.left_context_frames,
                right_context_frames=take.right_context_frames,
                gap_frames=take.gap_frames,
                end_offset_x=take.end_offset_x,
                end_offset_z=take.end_offset_z,
                align_heading=take.align_heading,
                length=int(take.length),
                offset=int(take.offset),
                progress=take.progress,
            )
            for take in clip.takes
        ),
    )


def restore_clip(store, payload: ClipData, start: int):
    clip = add_clip(store, start, start + payload.length, name=payload.name)
    for take_payload in payload.takes:
        takes.restore_take(store, clip, take_payload)
    if payload.active_take_uid:
        takes.select_take(clip, payload.active_take_uid)
    return clip


def create_clip_at(store, frame: float, default_length: int):
    interval = new_range(store.clips, frame, default_length)
    if interval is None:
        return None
    clip = add_clip(store, *interval)
    store.data.active_clip_uid = clip.uid
    return clip


def fill_gap(store, frame: float, default_length: int, scene_start: int, scene_end: int) -> list:
    ranges = fill_ranges(store.clips, frame, default_length, scene_start, scene_end)
    return [add_clip(store, start, end) for start, end in ranges]


def shrink_neighbors(store, cursor_frame: int, current_frame: int) -> int:
    previous = max(
        (clip for clip in store.clips if clip.end <= cursor_frame),
        key=lambda clip: clip.end, default=None,
    )
    following = min(
        (clip for clip in store.clips if clip.start > cursor_frame),
        key=lambda clip: clip.start, default=None,
    )
    changed = 0
    if previous is not None and previous.start < current_frame < previous.end:
        previous.end = current_frame
        changed += 1
    if following is not None and following.start < current_frame < following.end:
        _set_start_preserving_takes(following, current_frame)
        changed += 1
    apply_bindings(store)
    return changed


def move_clip(store, uid: int, new_start: int) -> bool:
    clip = find_clip(store.clips, uid)
    if clip is None:
        return False
    new_start = int(new_start)
    new_end = new_start + clip.end - clip.start
    if not is_free(store.clips, new_start, new_end, uid):
        return False
    if new_start != clip.start:
        clip.start, clip.end = new_start, new_end
        clip.bind_start = clip.bind_end = False
    apply_bindings(store)
    return True


def resize_start(store, uid: int, requested_start: int, *, detach_binding: bool = False) -> bool:
    clip = find_clip(store.clips, uid)
    if clip is None:
        return False
    start = min(int(requested_start), clip.end - TimelineConfig.MIN_CLIP_LENGTH)
    limit = left_limit(store.clips, uid)
    if limit is not None:
        start = max(start, limit)
    if start != clip.start:
        _set_start_preserving_takes(clip, start)
        if detach_binding:
            clip.bind_start = False
    apply_bindings(store)
    return True


def resize_end(store, uid: int, requested_end: int, *, detach_binding: bool = False) -> bool:
    clip = find_clip(store.clips, uid)
    if clip is None:
        return False
    end = max(int(requested_end), clip.start + TimelineConfig.MIN_CLIP_LENGTH)
    limit = right_limit(store.clips, uid)
    if limit is not None:
        end = min(end, limit)
    if end != clip.end:
        clip.end = end
        if detach_binding:
            clip.bind_end = False
    apply_bindings(store)
    return True


def set_duration(store, uid: int, duration: int) -> bool:
    clip = find_clip(store.clips, uid)
    if clip is None:
        return False
    return resize_end(store, uid, clip.start + max(TimelineConfig.MIN_CLIP_LENGTH, int(duration)))


def resize_shared_border(store, left_uid: int, right_uid: int, delta: int) -> bool:
    left = find_clip(store.clips, left_uid)
    right = find_clip(store.clips, right_uid)
    if left is None or right is None:
        return False
    delta = max(
        left.start + TimelineConfig.MIN_CLIP_LENGTH - left.end,
        min(right.end - TimelineConfig.MIN_CLIP_LENGTH - right.start, int(delta)),
    )
    if delta:
        left.end += delta
        _set_start_preserving_takes(right, right.start + delta)
    apply_bindings(store)
    return True


def stick_to_neighbors(store, uid: int, mode: str) -> bool:
    clip = find_clip(store.clips, uid)
    if clip is None:
        return False
    previous, following = neighbors(store.clips, uid)
    changed = False
    if mode in {"PREV", "BOTH"} and previous is not None:
        _set_start_preserving_takes(clip, previous.end)
        clip.bind_start = True
        changed = True
    if mode in {"NEXT", "BOTH"} and following is not None:
        clip.end = following.start
        clip.bind_end = True
        changed = True
    apply_bindings(store)
    return changed


def paste_clip(store, payload: ClipData, frame: int):
    if not is_free(store.clips, frame, frame + payload.length):
        return None
    clip = restore_clip(store, payload, frame)
    store.data.active_clip_uid = clip.uid
    return clip


def begin_drag(store, kind: str, uid: int, other_uid: int = 0) -> DragSession:
    store.data.active_clip_uid = uid
    return DragSession(kind, uid, other_uid, snapshot(store))


def update_drag(store, session: DragSession, delta_frames: int) -> bool:
    if find_clip(store.clips, session.uid) is None:
        return False
    original = session.initial[session.uid]
    restore_snapshot(store, session.initial)
    if session.kind == "BODY":
        if move_clip(store, session.uid, original.start + delta_frames):
            session.last_valid_delta = delta_frames
            return True
        return move_clip(store, session.uid, original.start + session.last_valid_delta)
    if session.kind == "LEFT":
        return resize_start(
            store, session.uid, original.start + delta_frames, detach_binding=True,
        )
    if session.kind == "RIGHT":
        return resize_end(store, session.uid, original.end + delta_frames, detach_binding=True)
    if session.kind == "SYNC":
        return resize_shared_border(store, session.uid, session.other_uid, delta_frames)
    return False


def cancel_drag(store, session: DragSession) -> None:
    restore_snapshot(store, session.initial)
