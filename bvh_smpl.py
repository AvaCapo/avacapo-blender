"""Conversion and concatenation of AvaCapo BVH motion for SMPL-X."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from io import BytesIO

import numpy as np

from broom.bvh import load_bvh_document_from_bytes
from broom.bvh.ops import trim_frames
from broom.bvh.retargeting import retarget_bvh_to_smplx
from broom.bvh.schemas import BVHDocument

from .retarget_maps import SMPLX_TO_SMPL_BVH
from .config import Config
from .bvh_cache import sanitize_bvh_document

config = Config()


SMPL_BVH_JOINT_NAMES = tuple(source for _, source, _ in SMPLX_TO_SMPL_BVH)

# Source BVH: Y-up and +Z forward. Target AMASS: Z-up and +Y forward.
Y_UP_TO_AMASS_Z_UP = np.array(
    [
        [-1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
    ],
    dtype=np.float64,
)


@dataclass(frozen=True)
class InMemorySmplNpz:
    """One converted NPZ payload retained only in process memory."""

    data: bytes
    source_start_frame: int
    source_end_frame: int
    frame_count: int
    fps: float


def _validate_direct_smpl_bvh(document: BVHDocument) -> None:
    """Check that the source BVH is a direct SMPL24 layout with no missing joints.
    """
    missing = [name for name in SMPL_BVH_JOINT_NAMES if name not in document.joint_index]
    if missing:
        raise ValueError(
            "BVH is not the supported SMPL24 layout; missing joints: "
            + ", ".join(missing)
        )

    for target_index, (_, source_name, target_parent) in enumerate(SMPLX_TO_SMPL_BVH):
        joint = document.joints[document.joint_index[source_name]]
        actual_parent = None if joint.parent < 0 else document.joints[joint.parent].name
        expected_parent = (
            None if target_parent < 0 else SMPLX_TO_SMPL_BVH[target_parent][1]
        )
        if actual_parent != expected_parent:
            raise ValueError(
                f"Joint {source_name!r} has parent {actual_parent!r}; "
                f"expected {expected_parent!r} for a direct SMPL BVH"
            )

        rotation_channels = [
            channel for channel in joint.channels if channel.endswith("rotation")
        ]
        if len(rotation_channels) != 3:
            raise ValueError(
                f"Joint {source_name!r} must have three rotation channels; "
                f"found {len(rotation_channels)}"
            )

        channel_values = document.motion_values[
            :, joint.channel_start : joint.channel_start + len(joint.channels)
        ]
        if not np.isfinite(channel_values).all():
            raise ValueError(
                f"Required body joint {source_name!r} contains NaN or infinite values"
            )

        if target_index == 0:
            position_channels = [
                channel for channel in joint.channels if channel.endswith("position")
            ]
            if len(position_channels) != 3:
                raise ValueError("The Hips root must have three position channels")


def _normalize_frame_slice(
    document: BVHDocument,
    start_frame: int,
    end_frame: int | None,
) -> tuple[int, int]:
    
    end = document.frame_count if end_frame is None else int(end_frame)
    if start_frame < 0:
        raise ValueError(f"start_frame must be non-negative, got {start_frame}")
    if end > document.frame_count:
        raise ValueError(
            f"end_frame {end} exceeds source frame count {document.frame_count}"
        )
    if end <= start_frame:
        raise ValueError(f"Frame slice must not be empty, got {start_frame}..{end}")
    return start_frame, end


def convert_bvh_smpl(
    document: BVHDocument,
    *,
    start_frame: int = 0,
    end_frame: int | None = None,
    target_fps: float | None = config.DEFAULT_FPS,
    root_scale: float = config.ROOT_TRANSLATION_SCALE,
    keep_y_up: bool = config.KEEP_Y_UP,
    gender: str = config.GENDER,
) -> dict[str, np.ndarray]:
    """Convert an in-memory source frame slice to SMPL-X arrays.

    Frame indices are zero-based and ``end_frame`` is exclusive.
    """

    if not np.isfinite(root_scale) or root_scale <= 0.0:
        raise ValueError("root_scale must be finite and positive")

    start, end = _normalize_frame_slice(document, start_frame, end_frame)
    selected = trim_frames(
        document,
        start_frame=start,
        end_frame=end,
    )
    _validate_direct_smpl_bvh(selected)

    result = retarget_bvh_to_smplx(
        document=selected,
        source_joint_names=SMPL_BVH_JOINT_NAMES,
        smplx_to_source=SMPLX_TO_SMPL_BVH,
        source_global_rotation_offsets=None,
        root_translation_scale=root_scale,
        coordinate_transform=None if keep_y_up else Y_UP_TO_AMASS_Z_UP,
        target_fps=target_fps,
        betas=None,
        hand_pose=None,
        gender=gender,
    )

    if result["poses"].ndim != 2 or result["poses"].shape[1] != 165:
        raise ValueError(f"Unexpected poses shape: {result['poses'].shape}")
    if result["trans"].shape != (result["poses"].shape[0], 3):
        raise ValueError(f"Unexpected trans shape: {result['trans'].shape}")
    if not np.isfinite(result["poses"]).all() or not np.isfinite(result["trans"]).all():
        raise ValueError("Converted motion contains NaN or infinite values")
    if np.any(result["betas"]) or np.any(result["poses"][:, 75:]):
        raise ValueError("Expected neutral zero betas and hand pose")
    return result


# def convert_smpl_bvh_bytes_without_assets(
#     bvh_bytes: bytes | bytearray | memoryview,
#     **kwargs,
# ) -> dict[str, np.ndarray]:
#     """Parse and convert BVH bytes without touching the filesystem."""

#     document = load_bvh_document_from_bytes(bvh_bytes)
#     return convert_smpl_document_without_assets(document, **kwargs)


def create_smpl_npz_bytes(
    result: Mapping[str, np.ndarray],
) -> bytes:
    """Serialize SMPL-X arrays as NPZ bytes held entirely in memory."""

    required_keys = {"poses", "trans", "gender", "mocap_framerate", "betas"}
    missing = required_keys.difference(result)
    if missing:
        raise ValueError("SMPL-X result is missing keys: " + ", ".join(sorted(missing)))

    with BytesIO() as buffer:
        np.savez_compressed(buffer, **{key: result[key] for key in required_keys})
        return buffer.getvalue()


def concatenate_bvh_documents(
    *documents: BVHDocument,
    align_root_translation: bool = True,
) -> BVHDocument:
    """Join compatible motions while keeping the first document's skeleton."""

    if not documents:
        raise ValueError("At least one BVH document is required")

    reference = documents[0]
    reference_layout = tuple(
        (joint.name, joint.parent, joint.channels, joint.channel_start)
        for joint in reference.joints
    )
    segments: list[np.ndarray] = []

    root_joint = reference.joints[reference.joint_index[reference.root_name]]
    root_position_indices = tuple(
        root_joint.channel_start + index
        for index, channel in enumerate(root_joint.channels)
        if channel.endswith("position")
    )

    for document in documents:
        layout = tuple(
            (joint.name, joint.parent, joint.channels, joint.channel_start)
            for joint in document.joints
        )
        if layout != reference_layout or document.total_channels != reference.total_channels:
            raise ValueError("Cannot join BVH motions with different skeleton layouts")
        if document.frame_count <= 0:
            raise ValueError("Cannot join an empty BVH motion")

        document = sanitize_bvh_document(document)
        segment = np.asarray(document.motion_values, dtype=np.float64).copy()
        if align_root_translation and segments and root_position_indices:
            previous_root = segments[-1][-1, root_position_indices]
            current_root = segment[0, root_position_indices]
            segment[:, root_position_indices] += previous_root - current_root
        segments.append(segment)

    motion_values = np.concatenate(segments, axis=0)
    return replace(
        reference,
        motion_rows=(),
        motion_values=motion_values,
        declared_frames=int(motion_values.shape[0]),
    )
