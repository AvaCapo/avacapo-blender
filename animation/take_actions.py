"""Copy one Clip range between a Take Action and the Working Action.

Take Actions use local time. The Working Action uses scene time. These
functions only manipulate FCurve keyframe points; Action/Slot lookup belongs
to the UI generation path.
"""

import numpy as np


COORD_FIELDS = (
    "co",
    "handle_left",
    "handle_right",
)

ENUM_FIELDS = (
    "handle_left_type",
    "handle_right_type",
    "interpolation",
    "easing",
    "type",
)

FLOAT_FIELDS = (
    "amplitude",
    "back",
    "period",
)

BOOL_FIELDS = (
    "select_control_point",
    "select_left_handle",
    "select_right_handle",
)


def curve_times(curve):
    """Read sorted keyframe times through Blender's bulk API."""
    count = len(curve.keyframe_points)
    co = np.empty(count * 2, dtype=np.float32)
    if count:
        curve.keyframe_points.foreach_get("co", co)
    return co.reshape(-1, 2)[:, 0]


def read_keys_range(curve, start, end):
    """Read complete keyframe properties in the half-open range [start, end)."""
    points = curve.keyframe_points
    times = curve_times(curve)
    left = int(np.searchsorted(times, start))
    right = int(np.searchsorted(times, end))
    count = right - left

    data = {}
    for field in COORD_FIELDS:
        data[field] = np.empty((count, 2), dtype=np.float32)
    for field in ENUM_FIELDS:
        data[field] = []
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
    shifted = {
        field: value.copy() if isinstance(value, np.ndarray) else list(value)
        for field, value in data.items()
    }
    for field in COORD_FIELDS:
        shifted[field][:, 0] += delta
    return shifted


def translate_keys(curve, start, end, delta):
    """Move existing keys in [start, end) by ``delta`` scene frames."""
    points = curve.keyframe_points
    times = curve_times(curve)
    left = int(np.searchsorted(times, start))
    right = int(np.searchsorted(times, end))
    for index in range(left, right):
        point = points[index]
        point.co.x += delta
        point.handle_left.x += delta
        point.handle_right.x += delta
    points.sort()
    curve.update()


def write_key(point, data, index):
    """Write every tested Keyframe Point property after its curve is sorted."""
    point.interpolation = data["interpolation"][index]
    point.easing = data["easing"][index]
    point.type = data["type"][index]
    for field in FLOAT_FIELDS:
        setattr(point, field, float(data[field][index]))
    point.handle_left_type = data["handle_left_type"][index]
    point.handle_right_type = data["handle_right_type"][index]
    point.co = data["co"][index]
    point.handle_left = data["handle_left"][index]
    point.handle_right = data["handle_right"][index]
    for field in BOOL_FIELDS:
        setattr(point, field, bool(data[field][index]))


def read_one(point):
    """Snapshot one neighbouring key before a structural FCurve edit."""
    return {
        "co": tuple(point.co),
        "handle_left": tuple(point.handle_left),
        "handle_right": tuple(point.handle_right),
        **{field: getattr(point, field) for field in ENUM_FIELDS},
        **{field: float(getattr(point, field)) for field in FLOAT_FIELDS},
        **{field: bool(getattr(point, field)) for field in BOOL_FIELDS},
    }


def restore_one(point, state):
    """Restore a neighbouring key Blender may adjust during an edit."""
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


def replace_range(curve, incoming, start, end):
    """Replace [start, end), preserving complete states of its outer keys.

    Both existing and incoming keys must be sorted and have unique times.
    The implementation is the Blender 5.1.2-tested order: remove, add, sort,
    write properties, update, restore the two outer keys, update.
    """
    points = curve.keyframe_points
    times = curve_times(curve)
    incoming_times = incoming["co"][:, 0]
    incoming_count = len(incoming_times)

    if not np.all(np.diff(times) > 0):
        raise ValueError("Existing keyframes must be sorted and unique.")
    if incoming_count:
        if not np.all(np.diff(incoming_times) > 0):
            raise ValueError("Incoming keyframes must be sorted and unique.")
        if not np.all((incoming_times >= start) & (incoming_times < end)):
            raise ValueError("Incoming keyframes outside target range.")

    left = int(np.searchsorted(times, start))
    right = int(np.searchsorted(times, end))
    guards = []
    if left > 0:
        guards.append((float(times[left - 1]), read_one(points[left - 1])))
    if right < len(times):
        guards.append((float(times[right]), read_one(points[right])))

    for index in range(right - 1, left - 1, -1):
        points.remove(points[index], fast=True)

    if incoming_count:
        old_count = len(points)
        points.add(incoming_count)
        for index in range(incoming_count):
            points[old_count + index].co = incoming["co"][index]
        points.sort()

        times = curve_times(curve)
        insert_start = int(np.searchsorted(times, incoming_times[0]))
        actual_times = times[insert_start:insert_start + incoming_count]
        if not np.allclose(actual_times, incoming_times, rtol=0, atol=1e-4):
            raise RuntimeError("Blender did not place incoming keyframes at their requested times.")
        for index in range(incoming_count):
            write_key(points[insert_start + index], incoming, index)

    curve.update()
    if guards:
        times = curve_times(curve)
        for frame, state in guards:
            index = int(np.searchsorted(times, frame))
            if index >= len(times) or abs(times[index] - frame) >= 1e-4:
                raise RuntimeError(f"Boundary key missing at frame {frame}")
            restore_one(points[index], state)
        curve.update()


def materialize_take(working_curves, take_curves, clip_start, clip_end, take_offset):
    """Copy visible local Take keys into matching global Working curves."""
    origin = clip_start + take_offset
    local_start = clip_start - origin
    local_end = clip_end - origin
    for identity, take_curve in take_curves.items():
        working_curve = working_curves.get(identity)
        if working_curve is None:
            raise ValueError(f"Working Action has no curve for {identity!r}.")
        local_keys = read_keys_range(take_curve, local_start, local_end)
        replace_range(
            working_curve,
            shift_keys(local_keys, origin),
            clip_start,
            clip_end,
        )


def checkpoint_take(working_curves, take_curves, clip_start, clip_end, take_offset):
    """Copy edited global Working keys back to matching local Take curves."""
    origin = clip_start + take_offset
    local_start = clip_start - origin
    local_end = clip_end - origin
    for identity, take_curve in take_curves.items():
        working_curve = working_curves.get(identity)
        if working_curve is None:
            raise ValueError(f"Working Action has no curve for {identity!r}.")
        working_keys = read_keys_range(working_curve, clip_start, clip_end)
        replace_range(
            take_curve,
            shift_keys(working_keys, -origin),
            local_start,
            local_end,
        )
