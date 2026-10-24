"""Compound Timeline commands that keep Track metadata and Action windows consistent."""

from ..core import takes
from . import actions


def switch_take(track, clip, take_uid):
    """Make one Take visible, restoring the previous state if the switch fails."""
    if not any(candidate == clip for candidate in track.clips):
        raise ValueError("Clip does not belong to this Track.")

    target_take = takes.find_take(clip, take_uid)
    if target_take is None:
        raise ValueError(f"Take {take_uid} does not belong to this Clip.")
    active_take = takes.current_take(clip)
    if active_take is not None and active_take.uid == target_take.uid:
        return target_take

    working_action = track.working_action
    if working_action is None:
        raise ValueError("Working Action binding is incomplete.")
    working_slot = actions.find_slot(
        working_action, track.working_slot_handle, "Working Action",
    )
    target_action, target_slot = _take_binding(target_take)
    if target_action == working_action:
        raise ValueError("A Take Action cannot also be the Working Action.")

    # Preflight every Slot before any Action is changed.
    actions.read_slot_curves(working_action, working_slot)
    actions.read_slot_curves(target_action, target_slot)
    active_action = active_slot = None
    if active_take is not None:
        active_action, active_slot = _take_binding(active_take)
        if active_action == working_action:
            raise ValueError("A Take Action cannot also be the Working Action.")
        if active_action == target_action:
            raise ValueError("Different Takes cannot share one Take Action.")
        actions.read_slot_curves(active_action, active_slot)

    working_snapshot = actions.snapshot_range(
        working_action, working_slot, clip.start, clip.end,
    )
    active_snapshot = None
    if active_take is not None:
        active_origin = clip.start + active_take.offset
        active_snapshot = actions.snapshot_range(
            active_action,
            active_slot,
            clip.start - active_origin,
            clip.end - active_origin,
        )

    try:
        if active_take is not None:
            actions.checkpoint_take(
                working_action, working_slot, active_action, active_slot,
                clip.start, clip.end, active_take.offset,
            )
        actions.materialize_take(
            working_action, working_slot, target_action, target_slot,
            clip.start, clip.end, target_take.offset,
        )
    except Exception:
        try:
            actions.restore_range(
                working_action, working_slot, working_snapshot, clip.start, clip.end,
            )
            if active_snapshot is not None:
                actions.restore_range(
                    active_action,
                    active_slot,
                    active_snapshot,
                    clip.start - active_origin,
                    clip.end - active_origin,
                )
        except Exception as rollback_error:
            raise RuntimeError("Could not restore Actions after switching Take.") from rollback_error
        raise

    clip.active_take_uid = target_take.uid
    return target_take


def _take_binding(take):
    if take.take_action is None:
        raise ValueError(f"Take {take.uid} has no Take Action.")
    return take.take_action, actions.find_slot(
        take.take_action, take.take_slot_handle, f"Take {take.uid}",
    )
