"""Data structures for timeline copies, snapshots and drag sessions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TakeData:
    name: str
    prompt: str
    seed: int
    offset: int
    length: int
    progress: float


@dataclass(frozen=True)
class ClipData:
    name: str
    prompt: str
    seed: int
    length: int
    active_take: int
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
