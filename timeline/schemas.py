"""Data structures for timeline copies, snapshots and drag sessions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TakeData:
    uid: int
    name: str
    prompt: str
    generation_mode: str
    generation_name: str
    generation_model: str
    generation_in_place: bool
    duration_seconds: float
    fps: float
    temperature: float
    cycled: bool
    seed: int
    left_frame: int
    right_frame: int
    left_context_frames: int
    right_context_frames: int
    gap_frames: int
    end_offset_x: float
    end_offset_z: float
    align_heading: bool
    offset: int
    length: int
    progress: float


@dataclass(frozen=True)
class ClipData:
    name: str
    length: int
    prompt: str
    seed: int
    generation_model: str
    generation_temperature: float
    generation_cycled: bool
    generation_in_place: bool
    active_take_uid: int
    takes: tuple[TakeData, ...]


@dataclass(frozen=True)
class ClipSnapshot:
    start: int
    end: int
    bind_start: bool
    bind_end: bool
    take_offsets: tuple[tuple[int, int], ...]


@dataclass
class DragSession:
    kind: str
    uid: int
    other_uid: int
    initial: dict[int, ClipSnapshot]
    last_valid_delta: int = 0
