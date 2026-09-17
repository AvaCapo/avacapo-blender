"""Process-local source BVH and derived payload cache.

The cache deliberately never persists data to disk. It is cleared when the
add-on unloads and source takes must be regenerated after Blender restarts.
"""

from __future__ import annotations

from dataclasses import replace
from threading import RLock

import numpy as np

from broom.bvh import load_bvh_document_from_bytes
from broom.bvh.schemas import BVHDocument

from .config import Config
from .logger import log

config = Config()


_cache_lock = RLock()
_source_documents: dict[str, BVHDocument] = {}
_converted_payloads: dict[str, bytes] = {}


def sanitize_bvh_document(document: BVHDocument) -> BVHDocument:
    """Interpolate non-finite motion samples without changing valid channels."""

    motion_values = np.asarray(document.motion_values, dtype=np.float64)
    invalid = ~np.isfinite(motion_values)
    invalid_count = int(invalid.sum())
    if invalid_count == 0:
        return document

    repaired = motion_values.copy()
    frame_indices = np.arange(document.frame_count, dtype=np.float64)
    for channel_index in np.flatnonzero(invalid.any(axis=0)):
        finite = np.isfinite(repaired[:, channel_index])
        if finite.any():
            repaired[~finite, channel_index] = np.interp(
                frame_indices[~finite],
                frame_indices[finite],
                repaired[finite, channel_index],
            )
        else:
            repaired[:, channel_index] = 0.0

    log.warning(
        "Repaired %s NaN/Inf BVH channel samples across %s frames",
        invalid_count,
        document.frame_count,
    )
    return replace(document, motion_rows=(), motion_values=repaired)


def cache_bvh_document(
    action_name: str,
    bvh_bytes: bytes | bytearray | memoryview | BVHDocument,
) -> BVHDocument:
    """Parse and retain one action's source BVH only in process memory."""

    # TODO(retarget UX): source server takes are intentionally not retained
    # between Blender sessions yet; persist them only after retarget UX is set.
    document = (
        bvh_bytes
        if isinstance(bvh_bytes, BVHDocument)
        else load_bvh_document_from_bytes(bvh_bytes)
    )
    document = sanitize_bvh_document(document)
    with _cache_lock:
        _source_documents[action_name] = document
        _converted_payloads.pop(action_name, None)
    return document


def get_cached_bvh_document(action_name: str) -> BVHDocument | None:
    """Return the sanitized source BVH for an action, if this session has it."""

    with _cache_lock:
        document = _source_documents.get(action_name)
    if document is None:
        return None

    sanitized = sanitize_bvh_document(document)
    if sanitized is not document:
        with _cache_lock:
            _source_documents[action_name] = sanitized
    return sanitized


def invalidate_cached_action(action_name: str) -> None:
    """Discard cached source data after an action is edited in Blender."""

    with _cache_lock:
        _source_documents.pop(action_name, None)
        _converted_payloads.pop(action_name, None)


def convert_cached_action_range(
    action_name: str,
    *,
    start_frame: int,
    end_frame: int,
    target_fps: float | None = config.DEFAULT_FPS,
    root_scale: float = config.ROOT_TRANSLATION_SCALE,
    keep_y_up: bool = config.KEEP_Y_UP,
    gender: str = config.GENDER,
) -> bytes:
    """Convert a cached frame range and retain the resulting NPZ in memory."""

    document = get_cached_bvh_document(action_name)
    if document is None:
        raise RuntimeError(
            "The source BVH is no longer available in memory; regenerate this take"
        )

    # Imported lazily to keep cache ownership separate from BVH -> SMPL-X work.
    from .bvh_smpl import convert_bvh_smpl, create_smpl_npz_bytes

    result = convert_bvh_smpl(
        document,
        start_frame=start_frame,
        end_frame=end_frame,
        target_fps=target_fps,
        root_scale=root_scale,
        keep_y_up=keep_y_up,
        gender=gender,
    )
    npz_bytes = create_smpl_npz_bytes(result)
    with _cache_lock:
        _converted_payloads[action_name] = npz_bytes
    return npz_bytes


def get_cached_conversion(action_name: str) -> bytes | None:
    with _cache_lock:
        return _converted_payloads.get(action_name)


def pop_converted_npz_bytes(action_name: str) -> bytes | None:
    """Remove and return a converted payload, for example after an upload."""

    with _cache_lock:
        conversion = _converted_payloads.pop(action_name, None)
    return conversion


def clear_memory_cache() -> None:
    """Discard all process-local source BVHs and derived NPZ payloads."""

    with _cache_lock:
        _source_documents.clear()
        _converted_payloads.clear()
