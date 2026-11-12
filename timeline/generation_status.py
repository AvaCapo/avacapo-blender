"""Display helpers for generation status and saved Take history."""

from ..core.generation_models import TaskStatus, TERMINAL_STATUSES


STATUS_LABELS = {
    TaskStatus.QUEUED: "Pending",
    TaskStatus.SUBMITTING: "Submitting",
    TaskStatus.SERVER_PENDING: "Pending",
    TaskStatus.SERVER_PROCESSING: "Processing",
    TaskStatus.DOWNLOADING: "Downloading",
    TaskStatus.APPLYING: "Preparing animation",
    TaskStatus.SUCCEEDED: "Ready",
    TaskStatus.CANCELLED: "Cancelled",
    TaskStatus.FAILED: "Failed",
}

# Stage indicators only; the existing API supplies no numeric percentage.
STATUS_PROGRESS = {
    TaskStatus.QUEUED: 0.0,
    TaskStatus.SUBMITTING: 0.1,
    TaskStatus.SERVER_PENDING: 0.2,
    TaskStatus.SERVER_PROCESSING: 0.5,
    TaskStatus.DOWNLOADING: 0.9,
    TaskStatus.APPLYING: 0.95,
    TaskStatus.SUCCEEDED: 1.0,
    TaskStatus.CANCELLED: 0.0,
    TaskStatus.FAILED: 0.0,
}


def is_pending(take):
    return TaskStatus(take.generation_status) not in TERMINAL_STATUSES


def set_status(take, status, *, error=None):
    """Update the displayed stage, preserving errors unless explicitly supplied."""
    take.generation_status = status.value
    take.progress = STATUS_PROGRESS[status]
    if error is not None:
        take.generation_error = error


def take_description(take):
    """Describe the saved request, never the clip's editable generation settings."""
    mode = take.generation_mode
    lines = [
        f"{take.name} — {STATUS_LABELS[TaskStatus(take.generation_status)]}",
        f"Prompt: {take.prompt or '(none)'}",
        f"Model: {take.generation_model} · Mode: {mode}",
        f"Seed: {take.seed}",
    ]
    if mode in {'text', 'constraints'}:
        lines.append(f"Duration: {take.duration_seconds:g} s · FPS: {take.fps:g}")
        if mode == 'text':
            lines.append(f"Temperature: {take.temperature:g}")
        else:
            lines.append(f"Constraint frames: {take.constraint_frames}")
        lines.append(f"Cycled: {'Yes' if take.cycled else 'No'}")
    elif mode == 'inbetween':
        lines.extend((
            f"Gap: {take.gap_frames} frames",
            f"Source frames: {take.left_frame} / {take.right_frame}",
            f"Context frames: {take.left_context_frames} / {take.right_context_frames}",
            f"End offset: X {take.end_offset_x:g} · Z {take.end_offset_z:g}",
            f"Align heading: {'Yes' if take.align_heading else 'No'}",
        ))
    lines.extend((
        f"In Place: {'Yes' if take.generation_in_place else 'No'}",
        f"Enhance Prompt: {'Yes' if take.generation_enhance_prompt else 'No'}",
    ))
    if take.generation_error:
        lines.append(f"Error: {take.generation_error}")
    return "\n".join(lines)
