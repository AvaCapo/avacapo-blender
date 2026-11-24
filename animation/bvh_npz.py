"""In-memory conversion of AvaCapo BVH motion to NPZ payloads.

The module never writes BVH or NPZ data to disk. Source Broom Motion objects and
converted NPZ payloads live only in process memory and are discarded when the
add-on is reloaded, disabled, or explicitly cleared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from io import BytesIO

import numpy as np

from broom import Motion
from broom.adapters.body_model import convert_motion_to_ndarray, serialize_ndarray_npz


from .retarget_maps import NPZ_BVH_MAP
from ..common.config import Config


config = Config()


NPZ_BVH_JOINT_NAMES = tuple(source for _, source, _ in NPZ_BVH_MAP)

# Source BVH: Y-up and +Z forward. Target: Z-up and +Y forward.
Y_UP_TO_Z_UP = np.array(
    [
        [-1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
    ],
    dtype=np.float64,
)


def convert_motion_npz(
    motion: Motion,
    *,
    body_to_source: Sequence[tuple[str, str, int]] = NPZ_BVH_MAP,
    source_global_rotation_offsets: np.ndarray | None = None,
    target_fps: float | None = None,
    root_scale: float = config.ROOT_TRANSLATION_SCALE,
    keep_y_up: bool = config.KEEP_Y_UP,
    gender: str = config.GENDER,
) -> dict[str, np.ndarray]:
    """Convert a mapped Motion, including an oriented Blender rest hierarchy.

    Unlike the direct BVH entry point, mapped joints may have helper ancestors
    and arbitrary names. Rotation offsets follow the source order in the map.
    By default samples are preserved so constraint frame indices stay valid.
    Input coordinates must already use the canonical Y-up source frame.
    """
    if not np.isfinite(root_scale) or root_scale <= 0.0:
        raise ValueError("root_scale must be finite and positive")

    mapping = tuple(body_to_source)
    result = convert_motion_to_ndarray(
        motion=motion,
        source_joint_names=tuple(source for _, source, _ in mapping),
        body_to_source=mapping,
        source_global_rotation_offsets=source_global_rotation_offsets,
        root_translation_scale=root_scale,
        coordinate_transform=None if keep_y_up else Y_UP_TO_Z_UP,
        target_fps=target_fps,
        betas=None,
        hand_pose=None,
        gender=gender,
    )

    # Broom rounds rates within 0.05 of an integer (including 29.97/59.94).
    # Keep the actual source or explicitly requested timing in the payload.
    result["mocap_framerate"] = np.asarray(
        1.0 / motion.frame_time if target_fps is None else float(target_fps),
        dtype=np.float64,
    )

    if result["poses"].ndim != 2 or result["poses"].shape[1] != 165:
        raise ValueError(f"Unexpected poses shape: {result['poses'].shape}")
    if result["trans"].shape != (result["poses"].shape[0], 3):
        raise ValueError(f"Unexpected trans shape: {result['trans'].shape}")
    if not np.isfinite(result["poses"]).all() or not np.isfinite(result["trans"]).all():
        raise ValueError("Converted motion contains NaN or infinite values")
    if np.any(result["betas"]) or np.any(result["poses"][:, 66:]):
        raise ValueError("Expected neutral zero betas, face and hand pose")
    return result


def create_npz_bytes(result: Mapping[str, np.ndarray]) -> bytes:
    """Serialize arrays as compressed NPZ bytes using Broom."""
    # Broom only serializes standard body-model fields and drops our root flag.
    if "avacapo_trans_is_root_position" in result:
        with BytesIO() as buffer:
            np.savez_compressed(buffer, **result)
            return buffer.getvalue()
    return serialize_ndarray_npz(result)