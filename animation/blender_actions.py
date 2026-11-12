"""Small Blender Action, slot, and FCurve access primitives.

These helpers are deliberately independent of retargeting and timeline UI.
Reading never creates Blender animation data or slots; writing callers must
explicitly choose the ``ensure_*`` API when slot creation is intended.
"""

from __future__ import annotations

import math
import struct

import bpy
import bpy_extras.anim_utils


def find_action_slot_for_object(action, obj):
    """Return an existing Action slot for ``obj`` without mutating Blender data.

    A Slot is returned only when it is currently active on the exact object
    with the exact Action. Slot names, target type, or being the sole Slot do
    not establish object ownership and are not used as fallbacks here.
    """
    if action is None:
        return None
    animation_data = obj.animation_data
    active_slot = None
    if animation_data is not None and animation_data.action == action:
        active_slot = getattr(animation_data, "action_slot", None)
    if active_slot is not None and any(active_slot == slot for slot in action.slots):
        return active_slot
    return None


def ensure_action_slot_for_object(action, obj):
    """Return a writable Action slot for ``obj``, creating one only if needed.

    This is an explicit creation path for a new binding. It never reuses a
    merely convenient existing Slot: its name, type, or sole-slot status does
    not prove that it belongs to ``obj``.
    """
    slot = find_action_slot_for_object(action, obj)
    if slot is not None:
        return slot
    return action.slots.new(obj.id_type, name=obj.name)


def find_action_slot_by_handle(action, handle):
    """Return an existing Action slot by its numeric handle without mutation."""
    if action is None or handle <= 0:
        return None
    return next((slot for slot in action.slots if slot.handle == handle), None)


def require_armature(obj):
    """Return ``obj`` when it is an armature, otherwise raise a user-facing error."""
    if obj is None or obj.type != "ARMATURE":
        raise ValueError("Select an armature to own this clip.")
    return obj


def set_clip_action_binding(clip, armature, action, action_slot) -> None:
    """Persist one explicit target Armature → Action → Slot binding."""
    armature = require_armature(armature)
    if (action is None) != (action_slot is None):
        raise ValueError("A Clip binding must contain both an Action and an Action slot.")
    if action is not None:
        if not any(action_slot == slot for slot in action.slots):
            raise ValueError("The Action slot does not belong to the selected Action.")
    clip.target_armature = armature
    clip.working_action = action
    clip.working_action_slot_handle = action_slot.handle if action_slot is not None else 0


def capture_clip_action_binding(clip, armature) -> None:
    """Capture the selected armature's current Action and slot without creating either."""
    armature = require_armature(armature)
    animation_data = armature.animation_data
    action = animation_data.action if animation_data is not None else None
    action_slot = find_action_slot_for_object(action, armature) if action is not None else None
    if action_slot is None:
        action = None
    set_clip_action_binding(clip, armature, action, action_slot)


def validate_clip_action_binding(clip, *, allow_unbound: bool = False):
    """Validate one persisted Clip binding without creating or redirecting it.

    A Clip without both Action and Slot is an unbound first-apply state only
    when ``allow_unbound`` is true. A partially missing or stale persisted
    binding is always an error. A shared Action Slot is rejected, while the
    same Action remains valid for different Slots on different Armatures.
    """
    armature = require_armature(clip.target_armature)
    action = clip.working_action
    handle = int(clip.working_action_slot_handle)
    if action is None and handle == 0:
        if allow_unbound:
            return armature, None, None
        raise ValueError("Clip has no working Action binding.")
    if action is None or handle <= 0:
        raise ValueError("Clip has an incomplete or stale Action binding.")
    action_slot = find_action_slot_by_handle(action, handle)
    if action_slot is None:
        raise ValueError("Clip working Action no longer contains its saved Action slot.")
    target_id_type = getattr(action_slot, "target_id_type", None)
    if target_id_type is not None and target_id_type != armature.id_type:
        raise ValueError("Clip Action slot is incompatible with the target Armature type.")
    if clip.applied_take_uid and (
        clip.applied_action != action
        or int(clip.applied_action_slot_handle) != action_slot.handle
    ):
        raise ValueError("Clip applied output belongs to a different Action binding.")
    _require_unshared_action_slot(clip, armature, action, action_slot)
    return armature, action, action_slot


def _require_unshared_action_slot(clip, armature, action, action_slot) -> None:
    """Reject a Slot currently or persistently claimed by another object."""
    for obj in bpy.data.objects:
        if obj == armature:
            continue
        animation_data = obj.animation_data
        if (
            animation_data is not None
            and animation_data.action == action
            and getattr(animation_data, "action_slot", None) == action_slot
        ):
            raise ValueError(
                "Clip Action slot is shared with another object; refusing to edit it."
            )

    scene = getattr(clip, "id_data", None)
    clip_data = getattr(scene, "clip_data", None)
    for other_clip in getattr(clip_data, "clips", ()):
        if other_clip == clip or other_clip.target_armature == armature:
            continue
        if (
            other_clip.working_action == action
            and int(other_clip.working_action_slot_handle) == action_slot.handle
        ):
            raise ValueError(
                "Clip Action slot is already bound to another target Armature."
            )


def validate_editable_slot_layout(action, action_slot) -> tuple:
    """Return editable FCurves of one slot, or reject an unsafe layout.

    The current range writer supports zero or one keyframe channelbag for an
    Action slot. Multiple channelbags could blend multiple curve sources, so a
    partial clear/write would be unsafe. Curves with modifiers are also
    rejected before mutation because modifier-aware range semantics are not
    implemented yet.
    """
    if action is None or action_slot is None:
        raise ValueError("An Action and Action slot are required.")
    channelbags = []
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            bags = getattr(strip, "channelbags", None)
            if bags is None:
                raise ValueError("Action contains a strip without editable FCurves.")
            channelbags.extend(
                bag for bag in bags
                if bag.slot_handle == action_slot.handle
            )
    if len(channelbags) > 1:
        raise ValueError(
            "Clip Action slot has multiple channelbags; range editing is not supported."
        )
    if not channelbags:
        return ()

    curves = tuple(channelbags[0].fcurves)
    seen = set()
    for curve in curves:
        curve_id = (curve.data_path, int(curve.array_index))
        if curve_id in seen:
            raise ValueError(
                "Clip Action slot has duplicate FCurves for one data path and array index."
            )
        seen.add(curve_id)
        if len(curve.modifiers):
            raise ValueError(
                f"Range editing does not support FCurve modifiers: {curve.data_path}[{curve.array_index}]."
            )
        _validate_curve_points(curve)
    return curves


def validate_clip_action_layout(clip):
    """Preflight one Clip binding and return its Armature, Action, slot, and curves.

    This is the single read-only entry point for a future range transaction.
    Its result is valid only until Blender data changes; callers must run it
    again immediately before a mutation.
    """
    armature, action, action_slot = validate_clip_action_binding(clip)
    curves = validate_editable_slot_layout(action, action_slot)
    return armature, action, action_slot, curves


def clear_slot_keyframes_in_range(action, action_slot, start: float, end_exclusive: float) -> int:
    """Delete keys in timeline range ``[start, end_exclusive)`` from one slot.

    The range uses Blender timeline frame coordinates and has an excluded right
    boundary. This operation validates the complete supported slot layout
    before deleting the first key. Empty FCurves are retained so their group,
    extrapolation, and other curve-level settings are not discarded.
    """
    start = float(start)
    end_exclusive = float(end_exclusive)
    if not math.isfinite(start) or not math.isfinite(end_exclusive):
        raise ValueError("Keyframe range boundaries must be finite.")
    if end_exclusive <= start:
        raise ValueError("Keyframe range must not be empty.")
    curves = validate_editable_slot_layout(action, action_slot)
    removed = 0
    for curve in curves:
        point_indices = [
            index
            for index, point in enumerate(curve.keyframe_points)
            if start <= float(point.co.x) < end_exclusive
        ]
        # Removing an RNA keyframe reindexes the collection. Resolve every
        # point from its current index and work right-to-left so no later key
        # shifts before it is removed.
        for index in reversed(point_indices):
            curve.keyframe_points.remove(curve.keyframe_points[index])
        if point_indices:
            curve.update()
            removed += len(point_indices)
    return removed


def move_slot_keyframes_in_range(
    action,
    action_slot,
    start: float,
    end_exclusive: float,
    destination_start: float,
    *,
    finalize=None,
    rollback_finalize=None,
) -> int:
    """Move keys from ``[start, end_exclusive)`` to a same-length destination.

    All coordinates use Blender timeline frames. Keys in the destination range
    are replaced, while keys outside both ranges are preserved. Overlapping
    source and destination ranges are safe: source keys are copied before
    either range is cleared. A private in-memory snapshot restores the slot if
    key writes or the optional related-metadata ``finalize`` callback fails;
    it is not persistent Clip or Take data.
    """
    start, end_exclusive, destination_start = _validate_move_range(
        start,
        end_exclusive,
        destination_start,
    )
    if destination_start == start:
        return 0
    destination_end_exclusive = destination_start + end_exclusive - start
    curves = validate_editable_slot_layout(action, action_slot)
    snapshot = _snapshot_keyframes(curves)
    delta = destination_start - start
    moving = [
        (curve, point)
        for curve, points in snapshot
        for point in points
        if start <= point[0][0] < end_exclusive
    ]
    try:
        for curve in curves:
            _remove_curve_keys_in_ranges(
                curve,
                ((start, end_exclusive), (destination_start, destination_end_exclusive)),
            )
        inserted = []
        for curve, point in moving:
            moved_point = _offset_keyframe_snapshot(point, delta)
            _insert_keyframe_position(curve, moved_point)
            inserted.append((curve, moved_point))
        for curve in curves:
            curve.update()
        for curve, moved_point in inserted:
            _apply_keyframe_snapshot(
                _find_keyframe_by_coordinate(curve, moved_point[0]),
                moved_point,
            )
        if finalize is not None:
            finalize()
    except Exception:
        _restore_keyframes(snapshot)
        if rollback_finalize is not None:
            rollback_finalize()
        raise
    return len(moving)


def trim_slot_keyframes_to_range(
    action,
    action_slot,
    previous_start: float,
    previous_end_exclusive: float,
    current_start: float,
    current_end_exclusive: float,
    *,
    finalize=None,
    rollback_finalize=None,
) -> int:
    """Keep old Clip-owned keys that remain in the current half-open range.

    Only keys that were inside ``[previous_start, previous_end_exclusive)``
    and lie outside ``[current_start, current_end_exclusive)`` are removed.
    Keys already present in a newly expanded part of the current range are
    intentionally left unchanged: expanding a Clip changes ownership but does
    not manufacture or overwrite animation samples.
    """
    previous_start, previous_end_exclusive = _validate_keyframe_range(
        previous_start,
        previous_end_exclusive,
    )
    current_start, current_end_exclusive = _validate_keyframe_range(
        current_start,
        current_end_exclusive,
    )
    curves = validate_editable_slot_layout(action, action_slot)
    snapshot = _snapshot_keyframes(curves)
    removed = 0
    try:
        for curve in curves:
            point_indices = [
                index
                for index, point in enumerate(curve.keyframe_points)
                if (
                    previous_start <= float(point.co.x) < previous_end_exclusive
                    and not current_start <= float(point.co.x) < current_end_exclusive
                )
            ]
            for index in reversed(point_indices):
                curve.keyframe_points.remove(curve.keyframe_points[index])
            if point_indices:
                curve.update()
                removed += len(point_indices)
        if finalize is not None:
            finalize()
    except Exception:
        _restore_keyframes(snapshot)
        if rollback_finalize is not None:
            rollback_finalize()
        raise
    return removed


def shift_slot_keyframes_within_range(
    action,
    action_slot,
    start: float,
    end_exclusive: float,
    delta: float,
    *,
    finalize=None,
    rollback_finalize=None,
) -> int:
    """Shift keys within one Clip range and crop samples leaving that range.

    This is used when an active Take changes its placement while its Clip
    bounds stay fixed. All keys in the explicit Slot range are replaced by
    their shifted counterparts that still fall inside that same range. Keys
    outside the range are not inspected or changed.
    """
    start, end_exclusive = _validate_keyframe_range(start, end_exclusive)
    delta = float(delta)
    if not math.isfinite(delta):
        raise ValueError("Keyframe shift must be finite.")
    if delta == 0.0:
        return 0
    curves = validate_editable_slot_layout(action, action_slot)
    snapshot = _snapshot_keyframes(curves)
    moving = [
        (curve, _offset_keyframe_snapshot(point, delta))
        for curve, points in snapshot
        for point in points
        if (
            start <= point[0][0] < end_exclusive
            and start <= point[0][0] + delta < end_exclusive
        )
    ]
    try:
        for curve in curves:
            _remove_curve_keys_in_ranges(curve, ((start, end_exclusive),))
        for curve, point in moving:
            _insert_keyframe_position(curve, point)
        for curve in curves:
            curve.update()
        for curve, point in moving:
            _apply_keyframe_snapshot(_find_keyframe_by_coordinate(curve, point[0]), point)
        if finalize is not None:
            finalize()
    except Exception:
        _restore_keyframes(snapshot)
        if rollback_finalize is not None:
            rollback_finalize()
        raise
    return len(moving)


def write_prepared_keyframes_in_range(
    action,
    action_slot,
    start: float,
    end_exclusive: float,
    frame_times,
    prepared_curves,
) -> int:
    """Write validated prepared curve samples into an empty timeline range.

    ``frame_times`` are already Blender timeline coordinates; this primitive
    does not offset, stretch, or otherwise reinterpret them. Every time must
    belong to ``[start, end_exclusive)``. ``prepared_curves`` supplies objects
    with ``data_path``, ``group_name``, and per-frame vector ``samples``.

    The caller owns clearing or replacing the destination range. Rejecting a
    range which already contains a key prevents accidental mixing of old and
    new output. Creating a missing FCurve is intentional; no persistent range
    record is created.
    """
    start, end_exclusive = _validate_keyframe_range(start, end_exclusive)
    frame_times = tuple(frame_times)
    prepared_curves = tuple(prepared_curves)
    curves = validate_editable_slot_layout(action, action_slot)
    frame_times, prepared = _validate_prepared_keyframes(
        frame_times,
        prepared_curves,
        start,
        end_exclusive,
    )
    _require_empty_prepared_range(curves, start, end_exclusive)

    channelbag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(
        action,
        action_slot,
    )
    existing = {
        (curve.data_path, int(curve.array_index)): curve
        for curve in channelbag.fcurves
    }
    # TODO(performance): bulk allocation avoids the former per-key lookup
    # quadratic cost, but Action snapshots, range clears, and per-point RNA
    # assignment can still dominate long Clip rewrites. Measure those stages
    # in Blender before introducing a more specialised batch writer.
    updated_curves = set()
    for prepared_curve, axis, values in prepared:
        curve_key = (prepared_curve.data_path, axis)
        curve = existing.get(curve_key)
        if curve is None:
            curve_args = {
                "data_path": prepared_curve.data_path,
                "index": axis,
            }
            if prepared_curve.group_name is not None:
                curve_args["group_name"] = prepared_curve.group_name
            curve = channelbag.fcurves.new(**curve_args)
            existing[curve_key] = curve
        first_new_point = len(curve.keyframe_points)
        curve.keyframe_points.add(len(frame_times))
        for index, (frame, value) in enumerate(zip(frame_times, values)):
            point = curve.keyframe_points[first_new_point + index]
            point.co = (frame, value)
            point.interpolation = "LINEAR"
        updated_curves.add(curve)
    for curve in updated_curves:
        curve.update()
    return sum(len(values) for _prepared_curve, _axis, values in prepared)


def replace_slot_keyframes_in_range(
    action,
    action_slot,
    start: float,
    end_exclusive: float,
    frame_times,
    prepared_curves,
    *,
    finalize=None,
    rollback_finalize=None,
) -> int:
    """Replace one slot's keys in a range and commit related metadata atomically.

    All Action and prepared-output checks run before the first mutation. The
    optional ``finalize`` callback runs only after new keys are written. If
    writing or finalization fails, this function restores the complete selected
    slot snapshot and then invokes ``rollback_finalize`` for caller-owned
    metadata. Neither callback is UI logic.
    """
    start, end_exclusive = _validate_keyframe_range(start, end_exclusive)
    curves = validate_editable_slot_layout(action, action_slot)
    _validate_prepared_keyframes(frame_times, prepared_curves, start, end_exclusive)
    snapshot = _snapshot_slot_curves(curves)
    try:
        clear_slot_keyframes_in_range(action, action_slot, start, end_exclusive)
        written = write_prepared_keyframes_in_range(
            action,
            action_slot,
            start,
            end_exclusive,
            frame_times,
            prepared_curves,
        )
        if finalize is not None:
            finalize()
    except Exception:
        _restore_slot_curves(action, action_slot, snapshot)
        if rollback_finalize is not None:
            rollback_finalize()
        raise
    return written


def snapshot_slot_curves(action, action_slot) -> tuple:
    """Capture all supported curves of one slot for an outer transaction."""
    return _snapshot_slot_curves(validate_editable_slot_layout(action, action_slot))


def validate_prepared_keyframes_for_range(
    frame_times,
    prepared_curves,
    start: float,
    end_exclusive: float,
) -> None:
    """Validate prepared samples before an outer range transaction mutates keys."""
    start, end_exclusive = _validate_keyframe_range(start, end_exclusive)
    _validate_prepared_keyframes(frame_times, prepared_curves, start, end_exclusive)


def restore_slot_curves(action, action_slot, snapshot) -> None:
    """Restore a snapshot obtained by :func:`snapshot_slot_curves`."""
    _restore_slot_curves(action, action_slot, snapshot)


def iter_slot_fcurves(action, action_slot):
    """Yield FCurves from one explicit Action slot without changing the Action."""
    if action_slot is None:
        raise ValueError("An explicit Action slot is required to read FCurves.")
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                if channelbag.slot_handle == action_slot.handle:
                    yield from channelbag.fcurves


def _validate_curve_points(curve) -> None:
    for point in curve.keyframe_points:
        values = (*point.co, *point.handle_left, *point.handle_right)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError(
                f"FCurve has a non-finite key or handle coordinate: "
                f"{curve.data_path}[{curve.array_index}]."
            )


def _validate_move_range(start, end_exclusive, destination_start) -> tuple[float, float, float]:
    start, end_exclusive = _validate_keyframe_range(start, end_exclusive)
    destination_start = float(destination_start)
    if not math.isfinite(destination_start):
        raise ValueError("Keyframe range boundaries must be finite.")
    return start, end_exclusive, destination_start


def _validate_keyframe_range(start, end_exclusive) -> tuple[float, float]:
    values = tuple(float(value) for value in (start, end_exclusive))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Keyframe range boundaries must be finite.")
    if values[1] <= values[0]:
        raise ValueError("Keyframe range must not be empty.")
    return values


def _validate_prepared_keyframes(frame_times, prepared_curves, start, end_exclusive) -> tuple:
    frames = tuple(float(frame) for frame in frame_times)
    prepared_curves = tuple(prepared_curves)
    if not frames:
        if prepared_curves:
            raise ValueError("Prepared curves require at least one frame time.")
        return frames, ()
    if any(not math.isfinite(frame) for frame in frames):
        raise ValueError("Prepared keyframe times must be finite.")
    if any(not start <= frame < end_exclusive for frame in frames):
        raise ValueError("Prepared keyframe time is outside the destination range.")
    if any(right <= left for left, right in zip(frames, frames[1:])):
        raise ValueError("Prepared keyframe times must be strictly increasing.")
    stored_frames = tuple(_blender_frame_coordinate(frame) for frame in frames)
    if any(right <= left for left, right in zip(stored_frames, stored_frames[1:])):
        raise ValueError(
            "Prepared keyframe times collapse at Blender's single-precision timeline resolution."
        )

    validated = []
    seen = set()
    for prepared_curve in prepared_curves:
        data_path = prepared_curve.data_path
        samples = tuple(tuple(float(value) for value in sample) for sample in prepared_curve.samples)
        if not isinstance(data_path, str) or not data_path:
            raise ValueError("Prepared curve must have a non-empty data path.")
        if len(samples) != len(frames):
            raise ValueError(f"Prepared curve {data_path!r} does not match frame count.")
        if not samples:
            raise ValueError(f"Prepared curve {data_path!r} has no samples.")
        axis_count = len(samples[0])
        if axis_count < 1 or any(len(sample) != axis_count for sample in samples):
            raise ValueError(f"Prepared curve {data_path!r} has inconsistent sample dimensions.")
        if not all(math.isfinite(value) for sample in samples for value in sample):
            raise ValueError(f"Prepared curve {data_path!r} has non-finite sample values.")
        for axis in range(axis_count):
            curve_id = (data_path, axis)
            if curve_id in seen:
                raise ValueError(f"Prepared output has duplicate FCurve {data_path}[{axis}].")
            seen.add(curve_id)
            validated.append((prepared_curve, axis, tuple(sample[axis] for sample in samples)))
    return frames, tuple(validated)


def _require_empty_prepared_range(curves, start, end_exclusive) -> None:
    occupied = [
        f"{curve.data_path}[{curve.array_index}]"
        for curve in curves
        if any(start <= float(point.co.x) < end_exclusive for point in curve.keyframe_points)
    ]
    if occupied:
        raise ValueError(
            "Destination range must be cleared before writing prepared keys: "
            + ", ".join(occupied)
        )


def _snapshot_keyframes(curves) -> tuple:
    return tuple(
        (curve, tuple(_keyframe_snapshot(point) for point in curve.keyframe_points))
        for curve in curves
    )


def _snapshot_slot_curves(curves) -> tuple:
    """Capture all selected-slot curves needed for a range transaction rollback."""
    if not curves:
        return ()
    return tuple(
        (
            curve.data_path,
            int(curve.array_index),
            None if curve.group is None else curve.group.name,
            curve.extrapolation,
            bool(curve.mute),
            bool(curve.lock),
            bool(curve.hide),
            tuple(float(value) for value in curve.color),
            curve.color_mode,
            tuple(_keyframe_snapshot(point) for point in curve.keyframe_points),
        )
        for curve in curves
    )


def _restore_slot_curves(action, action_slot, snapshot) -> None:
    """Replace one slot's supported curves with its pre-transaction snapshot."""
    channelbag = bpy_extras.anim_utils.action_ensure_channelbag_for_slot(
        action,
        action_slot,
    )
    for curve in list(channelbag.fcurves):
        channelbag.fcurves.remove(curve)

    inserted = []
    for (
        data_path,
        array_index,
        group_name,
        extrapolation,
        mute,
        lock,
        hide,
        color,
        color_mode,
        points,
    ) in snapshot:
        curve_args = {
            "data_path": data_path,
            "index": array_index,
        }
        if group_name is not None:
            curve_args["group_name"] = group_name
        curve = channelbag.fcurves.new(**curve_args)
        curve.extrapolation = extrapolation
        curve.mute = mute
        curve.lock = lock
        curve.hide = hide
        curve.color_mode = color_mode
        curve.color = color
        for point in points:
            _insert_keyframe_position(curve, point)
            inserted.append((curve, point))
    for curve in {curve for curve, _point in inserted}:
        curve.update()
    for curve, point in inserted:
        _apply_keyframe_snapshot(
            _find_keyframe_by_coordinate(curve, point[0]),
            point,
        )


def _keyframe_snapshot(point) -> tuple:
    return (
        (float(point.co.x), float(point.co.y)),
        (float(point.handle_left.x), float(point.handle_left.y)),
        (float(point.handle_right.x), float(point.handle_right.y)),
        point.interpolation,
        point.handle_left_type,
        point.handle_right_type,
        point.type,
        point.easing,
        float(point.amplitude),
        float(point.back),
        float(point.period),
        bool(point.select_control_point),
        bool(point.select_left_handle),
        bool(point.select_right_handle),
    )


def _offset_keyframe_snapshot(snapshot, delta: float) -> tuple:
    co, handle_left, handle_right, *properties = snapshot
    return (
        (co[0] + delta, co[1]),
        (handle_left[0] + delta, handle_left[1]),
        (handle_right[0] + delta, handle_right[1]),
        *properties,
    )


def _remove_curve_keys_in_ranges(curve, ranges) -> None:
    point_indices = [
        index
        for index, point in enumerate(curve.keyframe_points)
        if any(start <= float(point.co.x) < end_exclusive for start, end_exclusive in ranges)
    ]
    for index in reversed(point_indices):
        curve.keyframe_points.remove(curve.keyframe_points[index])


def _restore_keyframes(snapshot) -> None:
    for curve, _points in snapshot:
        for index in reversed(range(len(curve.keyframe_points))):
            curve.keyframe_points.remove(curve.keyframe_points[index])
    inserted = []
    for curve, points in snapshot:
        for point in points:
            _insert_keyframe_position(curve, point)
            inserted.append((curve, point))
    for curve, _points in snapshot:
        curve.update()
    for curve, point_snapshot in inserted:
        _apply_keyframe_snapshot(
            _find_keyframe_by_coordinate(curve, point_snapshot[0]),
            point_snapshot,
        )


def _insert_keyframe_position(curve, snapshot):
    return _insert_keyframe_at(curve, snapshot[0])


def _insert_keyframe_at(curve, co):
    return curve.keyframe_points.insert(*co, options={"FAST"})


def _find_keyframe_by_coordinate(curve, co):
    """Find one post-update keyframe by its unique ``(frame, value)`` coordinate."""
    expected_frame = _blender_frame_coordinate(co[0])
    matches = [
        point
        for point in curve.keyframe_points
        if math.isclose(float(point.co.x), expected_frame, abs_tol=1e-7)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Could not unambiguously resolve an inserted keyframe after curve update: "
            f"{curve.data_path}[{curve.array_index}] at frame {expected_frame}."
        )
    return matches[0]


def _blender_frame_coordinate(frame: float) -> float:
    """Return the single-precision timeline coordinate stored by Blender RNA."""
    return struct.unpack("f", struct.pack("f", float(frame)))[0]


def _apply_keyframe_snapshot(point, snapshot) -> None:
    (
        co,
        handle_left,
        handle_right,
        interpolation,
        handle_left_type,
        handle_right_type,
        keyframe_type,
        easing,
        amplitude,
        back,
        period,
        select_control_point,
        select_left_handle,
        select_right_handle,
    ) = snapshot
    point.interpolation = interpolation
    point.handle_left_type = handle_left_type
    point.handle_right_type = handle_right_type
    point.type = keyframe_type
    point.easing = easing
    point.amplitude = amplitude
    point.back = back
    point.period = period
    point.handle_left = handle_left
    point.handle_right = handle_right
    point.select_control_point = select_control_point
    point.select_left_handle = select_left_handle
    point.select_right_handle = select_right_handle


def selected_keyframe_times(action, action_slot, *, precision: int = 6) -> list[float]:
    """Return distinct selected key times for one Action slot.

    Times are rounded only to coalesce visually identical selected keyframes
    for UI selection and validation. They are not suitable as coordinates for
    moving or rewriting animation keys.
    """
    return sorted(
        {
            round(float(point.co.x), precision)
            for curve in iter_slot_fcurves(action, action_slot)
            for point in curve.keyframe_points
            if point.select_control_point
        }
    )


def count_pose_curves_keyed_at(action, action_slot, left_frame: float, right_frame: float) -> int:
    """Count pose FCurves with a key on both specified timeline frames."""
    count = 0
    for curve in iter_slot_fcurves(action, action_slot):
        if not curve.data_path.startswith('pose.bones["'):
            continue
        keyed_times = (float(point.co.x) for point in curve.keyframe_points)
        has_left = has_right = False
        for frame in keyed_times:
            has_left |= abs(frame - left_frame) <= 1e-6
            has_right |= abs(frame - right_frame) <= 1e-6
            if has_left and has_right:
                count += 1
                break
    return count
