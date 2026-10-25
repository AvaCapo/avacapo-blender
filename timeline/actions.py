"""Read and write one known Action Slot for Timeline Take operations."""

import numpy as np
import bpy_extras.anim_utils


COORD_FIELDS = ("co", "handle_left", "handle_right")
ENUM_FIELDS = (
    "handle_left_type", 
    "handle_right_type", 
    "interpolation", 
    "easing", 
    "type",
)
FLOAT_FIELDS = ("amplitude", "back", "period")
BOOL_FIELDS = (
    "select_control_point", 
    "select_left_handle", 
    "select_right_handle"
)


def find_slot(action, handle, label):
    """Read one unambiguous Slot by its saved handle without creating anything."""
    slots = [slot for slot in action.slots if slot.handle == handle] if action is not None else []
    if len(slots) != 1:
        raise ValueError(f"{label} Slot is missing or ambiguous.")
    return slots[0]


def _channelbags(action, slot):
    bags = []
    for layer in action.layers:
        for strip in layer.strips:
            channelbags = getattr(strip, "channelbags", None)
            if channelbags is None:
                raise ValueError("Action contains a strip without editable FCurves.")
            bags.extend(bag for bag in channelbags if bag.slot_handle == slot.handle)
    if len(bags) > 1:
        raise ValueError("Action Slot has multiple channelbags; Timeline range editing is unsupported.")
    return bags


def read_slot_curves(action, slot):
    """Return validated FCurves of a known Slot without creating a channelbag."""
    result = _slot_curves(action, slot)
    for identity, curve in result.items():
        times = curve_times(curve)
        if not np.all(np.diff(times) > 0):
            raise ValueError(f"FCurve keys must be sorted and unique: {identity!r}.")
    return result


def _slot_curves(action, slot):
    """Return known Slot curves without reading every keyframe time."""
    bags = _channelbags(action, slot)
    curves = () if not bags else tuple(bags[0].fcurves)
    result = {}
    for curve in curves:
        identity = (curve.data_path, int(curve.array_index))
        if identity in result:
            raise ValueError(f"Action Slot has duplicate FCurve {identity!r}.")
        if curve.modifiers:
            raise ValueError(f"Timeline range editing does not support FCurve modifiers: {identity!r}.")
        result[identity] = curve
    return result


def ensure_slot_channelbag(action, slot):
    """Create the channelbag only for an explicit write operation."""
    bags = _channelbags(action, slot)
    if bags:
        return bags[0]
    return bpy_extras.anim_utils.action_ensure_channelbag_for_slot(action, slot)


def _create_curve(action, slot, identity, source_curve=None):
    """Create one preflight-validated missing FCurve in a known Slot."""
    group_name = ""
    if source_curve is not None and source_curve.group is not None:
        group_name = source_curve.group.name
    return ensure_slot_channelbag(action, slot).fcurves.new(
        identity[0], index=identity[1], group_name=group_name,
    )


def curve_times(curve):
    """Read sorted keyframe times through Blender's bulk API."""
    count = len(curve.keyframe_points)
    co = np.empty(count * 2, dtype=np.float32)
    if count:
        curve.keyframe_points.foreach_get("co", co)
    return co.reshape(-1, 2)[:, 0]


def _empty_keys():
    return {
        **{field: np.empty((0, 2), dtype=np.float32) for field in COORD_FIELDS},
        **{field: [] for field in ENUM_FIELDS},
        **{field: np.empty(0, dtype=np.float32) for field in FLOAT_FIELDS},
        **{field: np.empty(0, dtype=bool) for field in BOOL_FIELDS},
    }


def read_keys_range(curve, start, end):
    """Read complete keyframe properties in the half-open range [start, end)."""
    points = curve.keyframe_points
    times = curve_times(curve)
    left = int(np.searchsorted(times, start))
    right = int(np.searchsorted(times, end))
    count = right - left
    data = _empty_keys()
    for field in COORD_FIELDS:
        data[field] = np.empty((count, 2), dtype=np.float32)
    for field in FLOAT_FIELDS:
        data[field] = np.empty(count, dtype=np.float32)
    for field in BOOL_FIELDS:
        data[field] = np.empty(count, dtype=bool)
    for result_index, point_index in enumerate(range(left, right)):
        point = points[point_index]
        for field in COORD_FIELDS:
            data[field][result_index] = getattr(point, field)
        for field in ENUM_FIELDS:
            data[field].append(getattr(point, field))
        for field in FLOAT_FIELDS:
            data[field][result_index] = getattr(point, field)
        for field in BOOL_FIELDS:
            data[field][result_index] = getattr(point, field)
    return data


def shift_keys(data, delta):
    """Copy keys and shift both key and Bezier-handle times by ``delta``."""
    shifted = {field: value.copy() if isinstance(value, np.ndarray) else list(value) for field, value in data.items()}
    for field in COORD_FIELDS:
        shifted[field][:, 0] += delta
    return shifted


def _write_key(point, data, index, *, keyframe_type=None):
    point.interpolation = data["interpolation"][index]
    point.easing = data["easing"][index]
    point.type = data["type"][index] if keyframe_type is None else keyframe_type
    for field in FLOAT_FIELDS:
        setattr(point, field, float(data[field][index]))
    point.handle_left_type = data["handle_left_type"][index]
    point.handle_right_type = data["handle_right_type"][index]
    point.co = data["co"][index]
    point.handle_left = data["handle_left"][index]
    point.handle_right = data["handle_right"][index]
    for field in BOOL_FIELDS:
        setattr(point, field, bool(data[field][index]))


def _read_one(point):
    return {
        "co": tuple(point.co), "handle_left": tuple(point.handle_left),
        "handle_right": tuple(point.handle_right),
        **{field: getattr(point, field) for field in ENUM_FIELDS},
        **{field: float(getattr(point, field)) for field in FLOAT_FIELDS},
        **{field: bool(getattr(point, field)) for field in BOOL_FIELDS},
    }


def _restore_one(point, state):
    point.interpolation = state["interpolation"]
    point.easing = state["easing"]
    point.type = state["type"]
    for field in FLOAT_FIELDS:
        setattr(point, field, state[field])
    point.handle_left_type = state["handle_left_type"]
    point.handle_right_type = state["handle_right_type"]
    point.co = state["co"]
    point.handle_left = state["handle_left"]
    point.handle_right = state["handle_right"]
    for field in BOOL_FIELDS:
        setattr(point, field, state[field])


def replace_range(curve, incoming, start, end, *, keyframe_type=None):
    """Replace [start, end), preserving complete states of its outer keys."""
    # TODO(generated-key-ownership): Timeline materialize must preserve keys
    # whose type is not GENERATED instead of clearing the entire window.
    points = curve.keyframe_points
    times = curve_times(curve)
    if not np.all(np.diff(times) > 0):
        raise ValueError("Existing keyframes must be sorted and unique.")
    incoming_times = incoming["co"][:, 0]
    if len(incoming_times) and (
        not np.all(np.diff(incoming_times) > 0)
        or not np.all((incoming_times >= start) & (incoming_times < end))
    ):
        raise ValueError("Incoming keyframes must be sorted, unique, and inside the target range.")
    left = int(np.searchsorted(times, start))
    right = int(np.searchsorted(times, end))
    guards = []
    if left:
        guards.append((float(times[left - 1]), _read_one(points[left - 1])))
    if right < len(times):
        guards.append((float(times[right]), _read_one(points[right])))
    for index in range(right - 1, left - 1, -1):
        points.remove(points[index], fast=True)
    if len(incoming_times):
        for frame, value in incoming["co"]:
            points.insert(
                float(frame),
                float(value),
                options={'FAST'},
            )

        times = curve_times(curve)
        insert_start = int(np.searchsorted(times, incoming_times[0]))
        actual_times = times[insert_start:insert_start + len(incoming_times)]

        if not np.allclose(actual_times, incoming_times, rtol=0, atol=1e-4):
            raise RuntimeError("Incorrect key positions after FAST insert.")

        for index in range(len(incoming_times)):
            _write_key(
                points[insert_start + index], incoming, index,
                keyframe_type=keyframe_type,
            )
    curve.update()
    for frame, state in guards:
        times = curve_times(curve)
        index = int(np.searchsorted(times, frame))
        if index >= len(times) or abs(times[index] - frame) >= 1e-4:
            # TODO(investigate-boundary-guard): Blender can lose a preserved
            # outer key when a newly inserted key is almost at the range edge.
            raise RuntimeError(f"Boundary key missing at frame {frame}")
        _restore_one(points[index], state)
    if guards:
        curve.update()


def materialize_take(
    working_action,
    working_slot,
    take_action,
    take_slot,
    clip_start,
    clip_end,
    take_offset,
    *,
    keyframe_type="GENERATED",
):
    """Copy a local Take window into the Working Action across the full channel scope."""
    working = read_slot_curves(working_action, working_slot)
    take = read_slot_curves(take_action, take_slot)
    origin = clip_start + take_offset
    local_start, local_end = clip_start - origin, clip_end - origin
    for identity in sorted(working.keys() | take.keys()):
        target = working.get(identity)
        source = take.get(identity)
        if target is None:
            target = _create_curve(working_action, working_slot, identity, source)
        keys = _empty_keys() if source is None else shift_keys(read_keys_range(source, local_start, local_end), origin)
        replace_range(
            target,
            keys,
            clip_start,
            clip_end,
            keyframe_type=keyframe_type,
        )


def apply_resize_delta(
    working_action,
    working_slot,
    take_action,
    take_slot,
    old_start,
    old_end,
    new_start,
    new_end,
    take_origin,
    *,
    keyframe_type="GENERATED",
):
    """Apply only the changed part of one immutable Take to its Working window."""
    removed = _range_difference(old_start, old_end, new_start, new_end)
    added = _range_difference(new_start, new_end, old_start, old_end)
    if not removed and not added:
        return False

    working = _slot_curves(working_action, working_slot)
    take = _slot_curves(take_action, take_slot)
    changed = False
    for identity in sorted(working.keys() | take.keys()):
        target = working.get(identity)
        source = take.get(identity)
        if target is None:
            if source is None or not added:
                continue
            target = _create_curve(working_action, working_slot, identity, source)

        points = target.keyframe_points
        curve_changed = False
        for start, end in removed:
            left = _lower_bound(points, start)
            right = _lower_bound(points, end)
            for index in range(right - 1, left - 1, -1):
                points.remove(points[index], fast=True)
                curve_changed = True

        if source is not None:
            source_points = source.keyframe_points
            for start, end in added:
                left = _lower_bound(source_points, start - take_origin)
                right = _lower_bound(source_points, end - take_origin)
                for index in range(left, right):
                    point = points.insert(
                        float(source_points[index].co.x + take_origin),
                        float(source_points[index].co.y),
                        options={'FAST'},
                    )
                    _copy_key(point, source_points[index], take_origin, keyframe_type)
                    curve_changed = True

        if curve_changed:
            target.update()
            changed = True
    return changed


def translate_take_window(
    working_action,
    working_slot,
    take_action,
    take_slot,
    start,
    end,
    delta,
):
    """Move active-Take keys in one Working Action window by whole frames."""
    #NOTE: This is a separate realization of translate clip. 
    # Faster Blender-native function is realized for a clip drag operation in the timeline.
    if not delta or end <= start:
        return False

    working = _slot_curves(working_action, working_slot)
    take = _slot_curves(take_action, take_slot)
    changed = False
    for identity in take:
        curve = working.get(identity)
        if curve is None:
            continue

        points = curve.keyframe_points
        left = _lower_bound(points, start)
        right = _lower_bound(points, end)
        if left == right:
            continue
        for index in range(left, right):
            point = points[index]
            point.co.x += delta
            point.handle_left.x += delta
            point.handle_right.x += delta
        points.sort()
        curve.update()
        changed = True
    return changed


def _range_difference(start, end, other_start, other_end):
    ranges = []
    if start < other_start:
        ranges.append((start, min(end, other_start)))
    if other_end < end:
        ranges.append((max(start, other_end), end))
    return [(left, right) for left, right in ranges if left < right]


def _lower_bound(points, frame):
    left, right = 0, len(points)
    while left < right:
        middle = (left + right) // 2
        if points[middle].co.x < frame:
            left = middle + 1
        else:
            right = middle
    return left


def _copy_key(target, source, time_offset, keyframe_type):
    target.interpolation = source.interpolation
    target.easing = source.easing
    target.type = keyframe_type
    for field in FLOAT_FIELDS:
        setattr(target, field, getattr(source, field))
    target.handle_left_type = source.handle_left_type
    target.handle_right_type = source.handle_right_type
    target.co = (source.co.x + time_offset, source.co.y)
    target.handle_left = (
        source.handle_left.x + time_offset,
        source.handle_left.y,
    )
    target.handle_right = (
        source.handle_right.x + time_offset,
        source.handle_right.y,
    )
    for field in BOOL_FIELDS:
        setattr(target, field, getattr(source, field))


def checkpoint_take(working_action, working_slot, take_action, take_slot, clip_start, clip_end, take_offset):
    """Copy a Working Action window back to a local Take across the full channel scope."""
    # TODO(deprecated-immutable-takes): Take Actions are immutable. This remains
    # temporarily for reference and will be removed after Resize/Offset cleanup.
    working = read_slot_curves(working_action, working_slot)
    take = read_slot_curves(take_action, take_slot)
    origin = clip_start + take_offset
    local_start, local_end = clip_start - origin, clip_end - origin
    for identity in sorted(working.keys() | take.keys()):
        target = take.get(identity)
        source = working.get(identity)
        if target is None:
            target = _create_curve(take_action, take_slot, identity, source)
        keys = _empty_keys() if source is None else shift_keys(read_keys_range(source, clip_start, clip_end), -origin)
        replace_range(target, keys, local_start, local_end)


def clear_range(action, slot, start, end):
    """Remove keys in [start, end) from every curve of one known Slot."""
    if end <= start:
        return
    for curve in read_slot_curves(action, slot).values():
        replace_range(curve, _empty_keys(), start, end)


def bake_generated_range(action, slot, start, end):
    """Make generated keys in [start, end) ordinary editable keyframes."""
    if end <= start:
        return
    for curve in read_slot_curves(action, slot).values():
        times = curve_times(curve)
        left = int(np.searchsorted(times, start))
        right = int(np.searchsorted(times, end))
        changed = False
        for index in range(left, right):
            point = curve.keyframe_points[index]
            if point.type == "GENERATED":
                point.type = "KEYFRAME"
                changed = True
        if changed:
            curve.update()


def snapshot_range(action, slot, start, end):
    """Capture every channel window of a known Slot for local operation rollback."""
    return {
        identity: read_keys_range(curve, start, end)
        for identity, curve in read_slot_curves(action, slot).items()
    }


def restore_range(action, slot, snapshot, start, end):
    """Restore a snapshot window and remove channels created after that snapshot."""
    curves = read_slot_curves(action, slot)
    bag = ensure_slot_channelbag(action, slot)
    for identity, curve in tuple(curves.items()):
        if identity not in snapshot:
            bag.fcurves.remove(curve)
    for identity, keys in snapshot.items():
        replace_range(curves[identity], keys, start, end)
