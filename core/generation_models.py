"""Generation inputs, statuses and results; request validation belongs to the client."""

from dataclasses import dataclass, field, replace
from enum import Enum
import secrets

from ..common.config import Config


class GenerationMode(str, Enum):
    TEXT = "text"
    CONSTRAINTS = "constraints"
    INBETWEEN = "inbetween"


class TaskStatus(str, Enum):
    QUEUED = "pending"
    SUBMITTING = "loading"
    SERVER_PENDING = "server_pending"
    SERVER_PROCESSING = "server_processing"
    DOWNLOADING = "downloading"
    SUCCEEDED = "done"
    CANCELLED = "aborted"
    FAILED = "error"


TERMINAL_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.FAILED})


@dataclass(frozen=True)
class TextParameters:
    duration: float = Config.DEFAULT_DURATION
    fps: float = Config.DEFAULT_FPS
    temperature: float = Config.DEFAULT_TEMPERATURE
    cycled: bool = False
    seed: int = -1


@dataclass(frozen=True)
class PoseConstraint:
    motion_file: bytes = field(repr=False)
    constraint_type: str = "fullbody"
    source_frame: int = 0
    target_frame: int = 0
    joint_names: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "joint_names", tuple(self.joint_names))


@dataclass(frozen=True)
class ConstraintsParameters:
    num_frames: int = 150
    constraints: tuple[PoseConstraint, ...] = ()
    seed: int = -1
    text_weight: float = 2.0
    constraint_weight: float = 2.5
    first_heading: float = 0.0
    direction: tuple[float, float, float] | None = None
    cycled: bool = False

    def __post_init__(self):
        object.__setattr__(self, "constraints", tuple(self.constraints))
        if self.direction is not None:
            object.__setattr__(self, "direction", tuple(self.direction))


@dataclass(frozen=True)
class InbetweenParameters:
    left_motion: bytes = field(repr=False)
    right_motion: bytes = field(repr=False)
    left_frame: int = 0
    right_frame: int = 0
    left_context_frames: int = 1
    right_context_frames: int = 1
    gap_frames: int = 150
    seed: int = -1
    end_offset_x: float = 0.0
    end_offset_z: float = 0.0
    align_heading: bool = True


@dataclass(frozen=True)
class GenerationCommand:
    prompt: str = ""
    parameters: TextParameters | ConstraintsParameters | InbetweenParameters = field(
        default_factory=TextParameters
    )
    model: str = Config.DEFAULT_MODEL_TYPE
    name: str = ""
    in_place: bool = False

    @property
    def mode(self) -> GenerationMode:
        return {
            TextParameters: GenerationMode.TEXT,
            ConstraintsParameters: GenerationMode.CONSTRAINTS,
            InbetweenParameters: GenerationMode.INBETWEEN,
        }[type(self.parameters)]

    def resolve_seed(self) -> "GenerationCommand":
        """Resolve the random-seed marker once before the command enters the queue."""
        if self.parameters.seed == -1:
            return replace(
                self, parameters=replace(self.parameters, seed=secrets.randbelow(2**31 - 1) + 1)
            )
        return self


class GenerationError(RuntimeError):
    """A generation error with a safe message for the task history."""


class GenerationCancelled(RuntimeError):
    """Generation was cancelled locally."""


@dataclass(frozen=True)
class GenerationResult:
    task_id: str
    server_job_id: str
    bvh_bytes: bytes = field(repr=False)
    resolved_seed: int | None = None


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    runtime_id: str
    mode: GenerationMode
    name: str
    prompt: str
    status: TaskStatus
    server_job_id: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    resolved_seed: int | None
    error: str | None
