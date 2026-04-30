"""
https://claude.ai/chat/c8b15e9a-0e5c-4644-8837-a70cdef6dd22
"""

import bpy
import math
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Iterator

_BL_VERSION: tuple = bpy.app.version  # e.g. (4, 4, 0) or (4, 2, 0)


def _iter_fcurves(action: bpy.types.Action) -> Iterator:
    if _BL_VERSION < (4, 4, 0):
        yield from action.fcurves
    else:
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    yield from channelbag.fcurves


SIMPLIFICATION_TOLERANCE: float = 0.003
BEZIER_FIT_TOLERANCE: float = 0.002
MIN_KEY_GAP: int = 2
MAX_SUBDIVIDE_PASSES: int = 200
VALUE_AXIS_WEIGHT: float = 1.0
HANDLE_MODE: str = "CATMULL_ROM"  # 'CATMULL_ROM' | 'AUTO_CLAMPED'
TANGENT_TENSION: float = 0.5
AUTO_DETECT_CORNERS: bool = True
CORNER_ANGLE_THRESHOLD_DEG: float = 45.0
UNBAKED_SUFFIX: str = "_unbaked"


@dataclass(frozen=True)
class Sample:
    """One (frame, value) observation from a baked FCurve."""

    frame: float
    value: float


@dataclass
class KeyPoint:
    """A keyframe with computed Bezier handles."""

    frame: float
    value: float
    left_handle: Tuple[float, float] = field(default_factory=lambda: (0.0, 0.0))
    right_handle: Tuple[float, float] = field(default_factory=lambda: (0.0, 0.0))
    handle_type_left: str = "FREE"
    handle_type_right: str = "FREE"


def extract_samples(fcurve) -> List[Sample]:
    """Return all keyframe (frame, value) pairs from *fcurve*, in order."""
    return [Sample(kp.co.x, kp.co.y) for kp in fcurve.keyframe_points]


def _safe_range(values: List[float]) -> float:
    r = max(values) - min(values)
    return r if r > 1e-9 else 1.0


def frame_span(samples: List[Sample]) -> float:
    if len(samples) < 2:
        return 1.0
    return _safe_range([s.frame for s in samples])


def value_span(samples: List[Sample]) -> float:
    return _safe_range([s.value for s in samples])


def normalise(samples: List[Sample]) -> List[Tuple[float, float]]:
    """
    Map samples into [0, 1]×[0, VALUE_AXIS_WEIGHT] for distance computations.
    """
    fs = frame_span(samples)
    vs = value_span(samples)
    f0 = samples[0].frame
    v0 = min(s.value for s in samples)
    return [((s.frame - f0) / fs, (s.value - v0) / vs * VALUE_AXIS_WEIGHT) for s in samples]


def _perp_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Perpendicular distance from point P to segment AB."""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _rdp(norm: List[Tuple[float, float]], lo: int, hi: int, tol: float, out: List[int]) -> None:
    """Recursive Ramer-Douglas-Peucker; appends interior indices to *out*."""
    if hi <= lo + 1:
        return
    ax, ay = norm[lo]
    bx, by = norm[hi]
    best_d, best_i = 0.0, lo
    for i in range(lo + 1, hi):
        d = _perp_dist(norm[i][0], norm[i][1], ax, ay, bx, by)
        if d > best_d:
            best_d, best_i = d, i
    if best_d > tol:
        _rdp(norm, lo, best_i, tol, out)
        out.append(best_i)
        _rdp(norm, best_i, hi, tol, out)


def rdp_simplify(samples: List[Sample], tol: float) -> List[int]:
    """
    Return sorted indices of the samples that survive RDP reduction.
    The first and last index are always included.
    """
    n = len(samples)
    if n <= 2:
        return list(range(n))
    norm = normalise(samples)
    interior: List[int] = []
    _rdp(norm, 0, n - 1, tol, interior)
    return sorted({0, *interior, n - 1})


def enforce_min_gap(indices: List[int], samples: List[Sample]) -> List[int]:
    """
    Drop indices that are fewer than MIN_KEY_GAP frames from their predecessor.
    The first and last index are always kept.
    """
    if len(indices) <= 2:
        return list(indices)
    first, *middle, last = indices
    result = [first]
    for idx in middle:
        if samples[idx].frame - samples[result[-1]].frame >= MIN_KEY_GAP:
            result.append(idx)
    result.append(last)
    return result


def _bez1(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    """Evaluate a scalar cubic Bezier at parameter t."""
    mt = 1.0 - t
    return mt**3 * p0 + 3.0 * mt**2 * t * p1 + 3.0 * mt * t**2 * p2 + t**3 * p3


def _find_t(frame: float, f0: float, hf1: float, hf2: float, f1: float) -> float:
    """
    Binary-search for the Bezier parameter t such that the x-coordinate
    of the 2-D Bezier equals *frame*.  Accurate to < 0.01 frames.
    """
    lo, hi = 0.0, 1.0
    for _ in range(64):
        t = 0.5 * (lo + hi)
        x = _bez1(f0, hf1, hf2, f1, t)
        if abs(x - frame) < 1e-4:
            return t
        if x < frame:
            lo = t
        else:
            hi = t
    return 0.5 * (lo + hi)


def bezier_value_at_frame(frame: float, kp0: KeyPoint, kp1: KeyPoint) -> float:
    """Reconstruct the Bezier value at *frame* for the segment kp0→kp1."""
    t = _find_t(frame, kp0.frame, kp0.right_handle[0], kp1.left_handle[0], kp1.frame)
    return _bez1(kp0.value, kp0.right_handle[1], kp1.left_handle[1], kp1.value, t)


def _catmull_slope(prev: Optional[Sample], curr: Sample, next_: Optional[Sample]) -> float:
    """
    Catmull-Rom slope at *curr* (dvalue/dframe), using ghost neighbours at
    endpoints.
    """
    p = prev if prev is not None else curr
    n = next_ if next_ is not None else curr
    dt = n.frame - p.frame
    if abs(dt) < 1e-9:
        return 0.0
    return TANGENT_TENSION * (n.value - p.value) / dt


def _handle_pair(
    prev: Optional[Sample], curr: Sample, next_: Optional[Sample]
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """
    Return (left_handle, right_handle) as absolute (frame, value) coordinates,
    placing handles at 1/3 of the distance to adjacent keys.
    """
    p = prev if prev is not None else curr
    n = next_ if next_ is not None else curr
    slope = _catmull_slope(prev, curr, next_)

    dt_l = (curr.frame - p.frame) / 3.0
    dt_r = (n.frame - curr.frame) / 3.0

    lh = (curr.frame - dt_l, curr.value - slope * dt_l)
    rh = (curr.frame + dt_r, curr.value + slope * dt_r)
    return lh, rh


def _is_corner(prev: Optional[Sample], curr: Sample, next_: Optional[Sample]) -> bool:
    """True when the in/out tangent angle exceeds CORNER_ANGLE_THRESHOLD_DEG."""
    if not AUTO_DETECT_CORNERS or prev is None or next_ is None:
        return False
    ix, iy = curr.frame - prev.frame, curr.value - prev.value
    ox, oy = next_.frame - curr.frame, next_.value - curr.value
    li, lo = math.hypot(ix, iy), math.hypot(ox, oy)
    if li < 1e-9 or lo < 1e-9:
        return False
    dot = max(-1.0, min(1.0, (ix * ox + iy * oy) / (li * lo)))
    return math.degrees(math.acos(dot)) > CORNER_ANGLE_THRESHOLD_DEG


def build_keypoints(samples: List[Sample], indices: List[int]) -> List[KeyPoint]:
    """
    Convert a list of sample indices into KeyPoints with computed handles.
    Handles are always computed (needed for Bezier error verification);
    if HANDLE_MODE != 'CATMULL_ROM' the actual handle type written to Blender
    will differ (see write_keypoints_to_fcurve).
    """
    ks = [samples[i] for i in indices]
    result: List[KeyPoint] = []
    for j, s in enumerate(ks):
        prev = ks[j - 1] if j > 0 else None
        next_ = ks[j + 1] if j < len(ks) - 1 else None
        lh, rh = _handle_pair(prev, s, next_)
        corner = _is_corner(prev, s, next_)
        ht = "VECTOR" if corner else "FREE"
        result.append(
            KeyPoint(
                frame=s.frame,
                value=s.value,
                left_handle=lh,
                right_handle=rh,
                handle_type_left=ht,
                handle_type_right=ht,
            )
        )
    return result


def _segment_max_error(
    seg_samples: List[Sample], kp0: KeyPoint, kp1: KeyPoint, vs: float
) -> Tuple[float, int]:
    """
    For every original sample strictly between kp0 and kp1, compute its
    deviation from the fitted Bezier.  Returns (max_error_normalised,
    local_index_of_worst_sample).  Local index is relative to seg_samples.
    """
    if len(seg_samples) <= 2:
        return 0.0, 1
    worst_err, worst_i = 0.0, 1
    for i, s in enumerate(seg_samples[1:-1], start=1):
        fitted = bezier_value_at_frame(s.frame, kp0, kp1)
        err = abs(s.value - fitted) / vs
        if err > worst_err:
            worst_err, worst_i = err, i
    return worst_err, worst_i


def adaptive_subdivide(samples: List[Sample], key_indices: List[int], vs: float) -> List[int]:
    """
    Walk every adjacent pair in *key_indices*, evaluate Bezier error, and
    insert extra keyframes wherever the error exceeds BEZIER_FIT_TOLERANCE.
    Repeats until all segments are within tolerance or MAX_SUBDIVIDE_PASSES
    is reached.
    """
    result = list(key_indices)
    passes = 0
    i = 0
    while i < len(result) - 1 and passes < MAX_SUBDIVIDE_PASSES:
        i0, i1 = result[i], result[i + 1]
        seg = samples[i0 : i1 + 1]
        kps = build_keypoints(samples, [i0, i1])
        err, worst_local = _segment_max_error(seg, kps[0], kps[1], vs)

        if err > BEZIER_FIT_TOLERANCE:
            worst_global = i0 + worst_local
            # Only insert if it respects the minimum gap on both sides
            gap_ok = (
                samples[worst_global].frame - samples[i0].frame >= MIN_KEY_GAP
                and samples[i1].frame - samples[worst_global].frame >= MIN_KEY_GAP
            )
            if worst_global not in result and gap_ok:
                result.insert(i + 1, worst_global)
                passes += 1
                # Re-check the left sub-segment without advancing i
                continue

        i += 1

    return result


def unbake_fcurve_samples(samples: List[Sample]) -> List[KeyPoint]:
    """
    Full unbaking pipeline for one FCurve worth of samples.
    Pure function: Sample list in, KeyPoint list out.
    """
    if len(samples) <= 2:
        return build_keypoints(samples, list(range(len(samples))))

    vs = value_span(samples)

    indices = rdp_simplify(samples, SIMPLIFICATION_TOLERANCE)
    indices = enforce_min_gap(indices, samples)
    indices = adaptive_subdivide(samples, indices, vs)
    indices = enforce_min_gap(indices, samples)

    return build_keypoints(samples, indices)


def write_keypoints(fcurve, keypoints: List[KeyPoint]) -> None:
    """
    Insert KeyPoints into a Blender FCurve.

    When HANDLE_MODE is 'CATMULL_ROM':  writes FREE/VECTOR handles explicitly.
    When HANDLE_MODE is 'AUTO_CLAMPED': lets Blender recalculate handles;
        our computed values are still used temporarily so Bezier error-checking
        earlier in the pipeline was accurate.
    """
    kfps = fcurve.keyframe_points
    kfps.add(len(keypoints))

    use_auto = HANDLE_MODE == "AUTO_CLAMPED"

    for i, kp in enumerate(keypoints):
        kf = kfps[i]
        kf.co = (kp.frame, kp.value)
        kf.interpolation = "BEZIER"

        if use_auto:
            # Blender will recompute handles; type overrides our computed values
            kf.handle_left_type = "AUTO_CLAMPED"
            kf.handle_right_type = "AUTO_CLAMPED"
        else:
            kf.handle_left_type = kp.handle_type_left
            kf.handle_right_type = kp.handle_type_right
            kf.handle_left = kp.left_handle
            kf.handle_right = kp.right_handle

    fcurve.update()


def _unbake_single_fcurve(fcurve) -> None:
    """In-place unbake of one Blender FCurve."""
    samples = extract_samples(fcurve)
    if not samples:
        return
    keypoints = unbake_fcurve_samples(samples)
    fcurve.keyframe_points.clear()
    write_keypoints(fcurve, keypoints)


def unbake(action: bpy.types.Action) -> bpy.types.Action:
    new_action = action.copy()
    new_action.name = action.name + UNBAKED_SUFFIX

    src_fcurves = list(_iter_fcurves(action))
    dst_fcurves = list(_iter_fcurves(new_action))

    original_totals: List[int] = []
    reduced_totals: List[int] = []

    for src_fc, dst_fc in zip(src_fcurves, dst_fcurves):
        original_totals.append(len(src_fc.keyframe_points))
        _unbake_single_fcurve(dst_fc)
        reduced_totals.append(len(dst_fc.keyframe_points))

    total_in = sum(original_totals)
    total_out = sum(reduced_totals)
    ratio = 100.0 * (1.0 - total_out / total_in) if total_in else 0.0
    print(
        f"[unbake] '{action.name}' → '{new_action.name}' | "
        f"{total_in} keys → {total_out} keys  "
        f"({ratio:.1f}% reduction,  {len(src_fcurves)} curves)"
    )
    return new_action


def unbake_active() -> Optional[bpy.types.Action]:
    obj = bpy.context.active_object
    if obj is None:
        print("[unbake] No active object.")
        return None
    if obj.animation_data is None or obj.animation_data.action is None:
        print("[unbake] Active object has no action.")
        return None

    original = obj.animation_data.action
    result = unbake(original)
    obj.animation_data.action = result
    return result


if __name__ == "__main__":
    unbake_active()
