"""Display helpers for the existing generation queue statuses."""

from ..core.generation_models import TaskStatus, TERMINAL_STATUSES


STATUS_LABELS = {
    TaskStatus.QUEUED: "Pending", TaskStatus.SUBMITTING: "Submitting",
    TaskStatus.SERVER_PENDING: "Pending", TaskStatus.SERVER_PROCESSING: "Processing",
    TaskStatus.DOWNLOADING: "Downloading", TaskStatus.SUCCEEDED: "Ready",
    TaskStatus.CANCELLED: "Cancelled", TaskStatus.FAILED: "Failed",
}

# Stage indicators only; the existing API supplies no numeric percentage.
STATUS_PROGRESS = {
    TaskStatus.QUEUED: 0.0, TaskStatus.SUBMITTING: 0.1, TaskStatus.SERVER_PENDING: 0.2,
    TaskStatus.SERVER_PROCESSING: 0.5, TaskStatus.DOWNLOADING: 0.9,
    TaskStatus.SUCCEEDED: 1.0, TaskStatus.CANCELLED: 0.0, TaskStatus.FAILED: 0.0,
}


def is_pending(take):
    return TaskStatus(take.generation_status) not in TERMINAL_STATUSES


def set_status(take, status, *, error=None):
    """Update the displayed stage, preserving errors unless explicitly supplied."""
    take.generation_status = status.value
    take.progress = STATUS_PROGRESS[status]
    if error is not None:
        take.generation_error = error
