"""
Clip / take track drawn over the Blender Timeline (Blender 5.2, gpu + blf).

Run it from the Text Editor (Alt+P) for a demo, or install it as an add-on.
Clips live in `scene.clip_track`, so they are saved with the .blend and
follow Blender's undo system.

Mouse (the "select button" follows Preferences > Keymap > Select With):
    select button   click = select, drag body = move, drag arrows = resize,
                    drag the dot between two touching clips = sync-resize,
                    double-click empty space = new clip + prompt popup,
                    double-click a clip = edit its prompt
    other button    context menu (in right-click-select mode: W, like Blender)
    Esc             cancel a drag

Hook for your backend: replace `GENERATE(scene, clip, take)` (bottom of
section 3). The default only animates the take's progress bar.
"""

bl_info = {
    "name": "Timeline Clip Track",
    "author": "",
    "version": (0, 2, 0),
    "blender": (5, 2, 0),
    "location": "Timeline / 3D Viewport > Sidebar > Clip Track",
    "description": "Clips with takes drawn over the Timeline",
    "category": "Animation",
}

import math
import os
from dataclasses import dataclass

import blf
import bpy
import gpu
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
    IntProperty, PointerProperty, StringProperty,
)
from bpy.types import Menu, Operator, Panel, PropertyGroup, UIList
from gpu_extras.batch import batch_for_shader


# ============================================================================
# 1. Style  (sizes are UI units, multiplied by the UI scale when drawing)
# ============================================================================

RGBA = tuple[float, float, float, float]


@dataclass(frozen=True)
class Palette:
    clip: RGBA        # outer rounded body + handles
    take_done: RGBA   # generated part of the take (the "percent")
    take_rest: RGBA   # not generated yet
    stripe: RGBA      # thin line between the two
    text: RGBA
    chevron: RGBA


@dataclass
class Style:
    clip_height: float = 36.0
    scrub_height: float = 23.0    # Blender's frame ruler at the top of the Timeline
    top_margin: float = 14.0      # gap between the ruler and the track
    handle_width: float = 15.0
    take_inset: float = 3.0       # take rect inset from the clip top/bottom
    corner_radius: float = 5.0
    clip_gap: float = 0.8         # visual gap on each side of a border
    chevron_size: float = 5.0
    chevron_width: float = 2.0
    dot_radius: float = 5.0
    sync_max_gap: int = 0         # frames between clips that still get a sync dot

    font_path: str = ""           # "" = Blender's UI font (e.g. point at IBM Plex Sans)
    font_size: float = 14.0
    bold: bool = True
    text_padding: float = 10.0

    idle: Palette = Palette(            # dimmer, like the right clip in the Figma
        clip=(0.47, 0.41, 0.49, 0.7), take_done=(0.38, 0.24, 0.40, 0.8),
        take_rest=(0.29, 0.31, 0.37, 1.0), stripe=(0.26, 0.31, 0.50, 0.8),
        text=(0.66, 0.57, 0.68, 0.8), chevron=(0.14, 0.13, 0.27, 0.3))
    selected: Palette = Palette(
        clip=(0.63, 0.55, 0.64, 0.8), take_done=(0.49, 0.29, 0.51, 0.9),
        take_rest=(0.33, 0.35, 0.42, 0.9), stripe=(0.30, 0.36, 0.58, 0.9),
        text=(0.76, 0.67, 0.78, 1.0), chevron=(0.14, 0.13, 0.27, 0.4))
    hover_tint: RGBA = (1.0, 1.0, 1.0, 0.16)
    dot_ring: RGBA = (0.86, 0.83, 0.90, 0.7)
    dot_fill: RGBA = (0.42, 0.42, 0.45, 0.7)


STYLE = Style()
HOVER = {"kind": "NONE", "uid": 0, "other": 0}   # what the mouse is over (for drawing)
MIN_LEN = 1                                      # shortest clip, in frames


# ============================================================================
# 2. Pure clip logic (works on any objects with .uid .start .end .takes
#    .bind_start .bind_end, so it is independent of bpy)
# ============================================================================

def ordered(clips):
    return sorted(clips, key=lambda c: (c.start, c.end))


def prev_next(clips, clip):
    o = ordered(clips)
    for i, c in enumerate(o):
        if c.uid == clip.uid:
            return (o[i - 1] if i > 0 else None,
                    o[i + 1] if i + 1 < len(o) else None)
    return None, None


def is_free(clips, start, end, ignore_uid=0):
    return all(not (start < c.end and end > c.start)
               for c in clips if c.uid != ignore_uid)


def gap_around(clips, frame):
    """(prev_end, next_start) of the free space containing `frame`.
    A side is None when it is unbounded; returns None if `frame` is inside a clip."""
    if any(c.start <= frame < c.end for c in clips):
        return None
    ends = [c.end for c in clips if c.end <= frame]
    starts = [c.start for c in clips if c.start > frame]
    return (max(ends) if ends else None, min(starts) if starts else None)


def new_clip_range(clips, frame, default_len):
    """New clip at `frame`: default length, cut off at the next clip."""
    f = math.floor(frame)
    gap = gap_around(clips, f)
    if gap is None:
        return None
    end = f + max(1, default_len)
    if gap[1] is not None:
        end = min(end, gap[1])
    return (f, end) if end > f else None


def fill_ranges(clips, frame, default_len, scene_start, scene_end):
    """Split the free gap under `frame` into clips of roughly `default_len`."""
    f = math.floor(frame)
    gap = gap_around(clips, f)
    if gap is None:
        return []
    default_len = max(1, default_len)
    lo = gap[0] if gap[0] is not None else min(scene_start, f)
    hi = gap[1] if gap[1] is not None else max(scene_end + 1, lo + default_len)
    if hi <= f:                                   # open end and cursor beyond it
        lo, hi = f, f + default_len
    total = hi - lo
    n = max(1, min(total, round(total / default_len)))
    base, rem = divmod(total, n)
    out, s = [], lo
    for i in range(n):
        e = s + base + (1 if i < rem else 0)
        out.append((s, e))
        s = e
    return out


def left_limit(clips, clip):
    """Smallest start `clip` may have (None = unbounded)."""
    p, _ = prev_next(clips, clip)
    if p is None:
        return None
    return p.start + MIN_LEN if p.bind_end else p.end


def right_limit(clips, clip):
    """Largest end `clip` may have (None = unbounded)."""
    _, n = prev_next(clips, clip)
    if n is None:
        return None
    return n.end - MIN_LEN if n.bind_start else n.start


def set_start(clip, new_start):
    """Move the left border; takes stay where they are in time (offset adapts)."""
    delta = new_start - clip.start
    clip.start = new_start
    for t in clip.takes:
        t.offset -= delta


def apply_bindings(clips):
    """Keep clips glued to their neighbours where the user asked for it."""
    for _ in range(len(clips) + 1):
        changed = False
        o = ordered(clips)
        for i, c in enumerate(o):
            if c.bind_start and i > 0:
                p = o[i - 1]
                if c.start != p.end and p.end < c.end:
                    set_start(c, p.end)
                    changed = True
            if c.bind_end and i + 1 < len(o):
                n = o[i + 1]
                if c.end != n.start and n.start > c.start:
                    c.end = n.start
                    changed = True
        if not changed:
            break


def snapshot(clips):
    return {c.uid: (c.start, c.end, c.bind_start, c.bind_end,
                    [t.offset for t in c.takes]) for c in clips}


def restore(clips, snap):
    for c in clips:
        s = snap.get(c.uid)
        if s is None:
            continue
        c.start, c.end, c.bind_start, c.bind_end = s[:4]
        for t, off in zip(c.takes, s[4]):
            t.offset = off


def active_take(clip):
    n = len(clip.takes)
    return clip.takes[min(max(clip.active_take, 0), n - 1)] if n else None


# ============================================================================
# 3. Data (saved in the .blend, undoable)
# ============================================================================

class _Guard:
    """While active, clip start/end are written raw (no validation / clamping)."""
    def __init__(self):
        self.depth = 0

    @property
    def busy(self):
        return self.depth > 0

    def __enter__(self):
        self.depth += 1

    def __exit__(self, *exc):
        self.depth -= 1
        return False


_guard = _Guard()


def _clips_of(clip):
    return list(clip.id_data.clip_track.clips)


def _get_start(self):
    return self.get("_start", 0)


def _set_start(self, value):
    if _guard.busy:
        self["_start"] = int(value)
        return
    with _guard:                                   # edited from the panel: clamp
        clips = _clips_of(self)
        v = min(int(value), self.get("_end", 1) - MIN_LEN)
        lo = left_limit(clips, self)
        if lo is not None:
            v = max(v, lo)
        set_start(self, v)
        apply_bindings(clips)


def _get_end(self):
    return self.get("_end", 1)


def _set_end(self, value):
    if _guard.busy:
        self["_end"] = int(value)
        return
    with _guard:
        clips = _clips_of(self)
        v = max(int(value), self.get("_start", 0) + MIN_LEN)
        hi = right_limit(clips, self)
        if hi is not None:
            v = min(v, hi)
        self.end = v
        apply_bindings(clips)


def _get_duration(self):
    return self.end - self.start


def _set_duration(self, value):
    self.end = self.start + max(MIN_LEN, int(value))     # goes through _set_end


def _on_bind_changed(self, context):
    if not _guard.busy:
        with _guard:
            apply_bindings(_clips_of(self))


class CT_Take(PropertyGroup):
    uid: IntProperty()
    name: StringProperty(name="Name", default="Take")
    prompt: StringProperty(name="Prompt")
    seed: IntProperty(name="Seed", min=0)
    offset: IntProperty(
        name="Offset", description="Frames from the clip start to the take start. "
        "Moving the clip does not change it")
    length: IntProperty(name="Length", min=1, default=1)
    progress: FloatProperty(name="Progress", min=0.0, max=1.0, default=1.0,
                            subtype='FACTOR')


class CT_Clip(PropertyGroup):
    uid: IntProperty()
    name: StringProperty(name="Name", default="Clip")
    prompt: StringProperty(name="Prompt")
    seed: IntProperty(name="Seed", min=0, description="0 = random")
    start: IntProperty(name="Start", get=_get_start, set=_set_start)
    end: IntProperty(name="End", get=_get_end, set=_set_end)
    duration: IntProperty(name="Duration", min=1, get=_get_duration, set=_set_duration)
    bind_start: BoolProperty(
        name="Stick to Previous Clip", update=_on_bind_changed,
        description="Keep the start glued to the end of the previous clip")
    bind_end: BoolProperty(
        name="Stick to Next Clip", update=_on_bind_changed,
        description="Keep the end glued to the start of the next clip")
    takes: CollectionProperty(type=CT_Take)
    active_take: IntProperty(default=0, min=0)
    take_counter: IntProperty(default=0)


class CT_Scene(PropertyGroup):
    clips: CollectionProperty(type=CT_Clip)
    active_uid: IntProperty(default=0)             # 0 = nothing selected
    next_uid: IntProperty(default=1)
    default_length: IntProperty(
        name="Default Length", min=1, default=60,
        description="Length of a new clip when there is room for it")


def find_clip(ct, uid):
    for c in ct.clips:
        if c.uid == uid:
            return c
    return None


def active_clip(ct):
    return find_clip(ct, ct.active_uid) if ct.active_uid else None


def add_clip(ct, start, end, name=None):
    clip = ct.clips.add()
    clip.uid = ct.next_uid
    ct.next_uid += 1
    clip.name = name or f"Clip {clip.uid}"
    with _guard:
        clip.start = start
        clip.end = end
    return clip


def add_take(clip, prompt=None):
    clip.take_counter += 1
    take = clip.takes.add()
    take.uid = clip.take_counter
    take.name = f"Take {len(clip.takes)}"
    take.prompt = clip.prompt if prompt is None else prompt
    take.seed = clip.seed
    take.offset = 0
    take.length = max(MIN_LEN, clip.end - clip.start)
    take.progress = 0.0
    clip.active_take = len(clip.takes) - 1
    return take


def remove_clip(ct, clip):
    uid = clip.uid
    p, n = prev_next(list(ct.clips), clip)
    with _guard:
        if p is not None and p.bind_end:
            p.bind_end = False
        if n is not None and n.bind_start:
            n.bind_start = False
    for i, c in enumerate(ct.clips):
        if c.uid == uid:
            ct.clips.remove(i)
            break
    if ct.active_uid == uid:
        ct.active_uid = 0


# --- take generation hook ----------------------------------------------------

def _simulate_generation(scene, clip, take):
    """Stand-in for the real backend: fills the progress bar over ~2 seconds."""
    names = (scene.name, clip.uid, take.uid)

    def tick():
        sc = bpy.data.scenes.get(names[0])
        ct = getattr(sc, "clip_track", None)
        c = find_clip(ct, names[1]) if ct is not None else None
        t = next((t for t in c.takes if t.uid == names[2]), None) if c is not None else None
        if t is None:
            return None                                # clip/take was deleted
        t.progress = min(1.0, t.progress + 1.0)
        tag_redraw()
        return None if t.progress >= 1.0 else 0.1

    bpy.app.timers.register(tick, first_interval=0.5)


GENERATE = _simulate_generation    # replace with: def GENERATE(scene, clip, take): ...


def tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'DOPESHEET_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()


# --- clipboard (session only) ---------------------------------------------------

_CLIPBOARD = {}


def _clip_to_dict(clip):
    return {
        "name": clip.name, "prompt": clip.prompt, "seed": clip.seed,
        "length": clip.end - clip.start, "active_take": clip.active_take,
        "takes": [{"name": t.name, "prompt": t.prompt, "seed": t.seed,
                   "offset": t.offset, "length": t.length, "progress": t.progress}
                  for t in clip.takes],
    }


def _clip_from_dict(ct, d, start):
    clip = add_clip(ct, start, start + d["length"], name=d["name"])
    clip.prompt, clip.seed = d["prompt"], d["seed"]
    for td in d["takes"]:
        t = add_take(clip, prompt=td["prompt"])
        t.name, t.seed, t.offset = td["name"], td["seed"], td["offset"]
        t.length, t.progress = td["length"], td["progress"]
    clip.active_take = d["active_take"]
    return clip


# ============================================================================
# 4. View, layout and hit-testing (shared by drawing and mouse operators)
# ============================================================================

def _ui_scale():
    system = bpy.context.preferences.system
    return getattr(system, "ui_scale", system.dpi * system.pixel_size / 72.0)


def _is_timeline(space):
    return (getattr(space, "ui_mode", None) == 'TIMELINE'
            or getattr(space, "mode", None) == 'TIMELINE')


def _in_timeline(context):
    sd, region = context.space_data, context.region
    return (sd is not None and sd.type == 'DOPESHEET_EDITOR' and _is_timeline(sd)
            and region is not None and region.type == 'WINDOW')


def select_button(context):
    """'LEFTMOUSE' or 'RIGHTMOUSE', from Preferences > Keymap > Select With."""
    kc = context.window_manager.keyconfigs.active
    prefs = getattr(kc, "preferences", None)
    return 'RIGHTMOUSE' if getattr(prefs, "select_mouse", 'LEFT') == 'RIGHT' else 'LEFTMOUSE'


class _View:
    """frame <-> region pixel, measured from the Timeline's own View2D."""
    def __init__(self, region):
        v2d = region.view2d
        a = v2d.view_to_region(0, 0, clip=False)[0]
        b = v2d.view_to_region(10000, 0, clip=False)[0]
        self.x0 = a
        self.ppf = (b - a) / 10000.0 or 1e-6      # pixels per frame

    def px(self, frame):
        return self.x0 + frame * self.ppf

    def frame(self, x):
        return (x - self.x0) / self.ppf


class _Metrics:
    def __init__(self, region, style, scale):
        self.scale = scale
        self.y1 = region.height - (style.scrub_height + style.top_margin) * scale
        self.y0 = self.y1 - style.clip_height * scale
        self.hw = style.handle_width * scale
        self.radius = style.corner_radius * scale
        self.inset = style.take_inset * scale
        self.gap = style.clip_gap * scale
        self.dot_r = style.dot_radius * scale
        self.stripe_w = max(2.0, 3.0 * scale)


def _view_metrics(region, style=None, scale=None):
    style = style or STYLE
    return _View(region), _Metrics(region, style, scale or _ui_scale())


@dataclass
class Hit:
    kind: str            # NONE | EMPTY | BODY | LEFT | RIGHT | SYNC
    clip: object = None  # SYNC: the left clip
    other: object = None  # SYNC: the right clip
    frame: float = 0.0


def hit_test(clips, view, m, style, mx, my):
    if not (m.y0 <= my <= m.y1):
        return Hit('NONE')
    frame = view.frame(mx)
    o = ordered(clips)
    for a, b in zip(o, o[1:]):                      # sync dots win near a shared border
        if b.start - a.end <= style.sync_max_gap:
            bx = (view.px(a.end) + view.px(b.start)) / 2.0
            if abs(mx - bx) <= m.dot_r + 2.0 * m.scale:
                return Hit('SYNC', a, b, frame)
    for c in o:
        x0, x1 = view.px(c.start), view.px(c.end)
        if x0 <= mx <= x1:
            hw = min(m.hw, (x1 - x0) * 0.3)
            kind = 'LEFT' if mx <= x0 + hw else 'RIGHT' if mx >= x1 - hw else 'BODY'
            return Hit(kind, c, None, frame)
    return Hit('EMPTY', None, None, frame)


# ============================================================================
# 5. Drawing
# ============================================================================

class _Mesh:
    """Collects triangles with per-vertex colour; drawn in one call."""
    def __init__(self):
        self.pos, self.col, self.idx = [], [], []

    def _poly(self, pts, color):                    # convex polygon, triangle fan
        i = len(self.pos)
        self.pos.extend(pts)
        self.col.extend([color] * len(pts))
        for k in range(1, len(pts) - 1):
            self.idx.append((i, i + k, i + k + 1))

    def rect(self, x0, y0, x1, y1, color):
        self._poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], color)

    def rounded_rect(self, x0, y0, x1, y1, r, color,
                     corners=(True, True, True, True), seg=6):
        """corners = (top-right, top-left, bottom-left, bottom-right) rounded?"""
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        if r < 0.75:
            return self.rect(x0, y0, x1, y1, color)
        arcs = ((x1 - r, y1 - r, 0, corners[0], (x1, y1)),
                (x0 + r, y1 - r, 90, corners[1], (x0, y1)),
                (x0 + r, y0 + r, 180, corners[2], (x0, y0)),
                (x1 - r, y0 + r, 270, corners[3], (x1, y0)))
        pts = []
        for cx, cy, a0, rounded, corner in arcs:
            if not rounded:
                pts.append(corner)
                continue
            for s in range(seg + 1):
                a = math.radians(a0 + 90.0 * s / seg)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        self._poly(pts, color)

    def circle(self, cx, cy, r, color, seg=20):
        self._poly([(cx + r * math.cos(2 * math.pi * i / seg),
                     cy + r * math.sin(2 * math.pi * i / seg)) for i in range(seg)], color)

    def line(self, p0, p1, t, color):
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = math.hypot(dx, dy) or 1.0
        ox, oy = -dy / n * t / 2.0, dx / n * t / 2.0
        self._poly([(p0[0] + ox, p0[1] + oy), (p1[0] + ox, p1[1] + oy),
                    (p1[0] - ox, p1[1] - oy), (p0[0] - ox, p0[1] - oy)], color)

    def chevron(self, cx, cy, s, direction, t, color):
        """direction -1 = '<', +1 = '>'."""
        tip = (cx + direction * s * 0.5, cy)
        self.line((cx - direction * s * 0.5, cy + s), tip, t, color)
        self.line(tip, (cx - direction * s * 0.5, cy - s), t, color)

    def draw(self):
        if not self.idx:
            return
        shader = gpu.shader.from_builtin('FLAT_COLOR')
        batch = batch_for_shader(shader, 'TRIS',
                                 {"pos": self.pos, "color": self.col}, indices=self.idx)
        batch.draw(shader)


_font_cache = {}


def _font_id(style):
    if not style.font_path:
        return 0
    if style.font_path not in _font_cache:
        fid = blf.load(style.font_path) if os.path.exists(style.font_path) else -1
        _font_cache[style.font_path] = fid if fid != -1 else 0
    return _font_cache[style.font_path]


def _fit_text(font_id, text, max_width):
    """Shorten `text` with an ellipsis so it fits into `max_width` pixels."""
    if max_width <= 0:
        return ""
    if blf.dimensions(font_id, text)[0] <= max_width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if blf.dimensions(font_id, text[:mid] + "\u2026")[0] <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "\u2026" if lo else ""


def draw_track(clips, style=None, active_uid=0, hover=None):
    """Draw the clip track on the Timeline region that is currently being drawn."""
    context = bpy.context
    space, region = context.space_data, context.region
    if space is None or region is None or not _is_timeline(space):
        return
    style = style or STYLE
    hover = hover or {}
    scale = _ui_scale()
    view, m = _view_metrics(region, style, scale)
    clips = ordered(clips)
    by_uid = {c.uid: c for c in clips}
    cy = (m.y0 + m.y1) / 2.0

    mesh = _Mesh()
    labels = []                                     # (text, x_from, x_to, colour)
    for c in clips:
        xa, xb = view.px(c.start), view.px(c.end)
        if xb < -m.hw or xa > region.width + m.hw:
            continue                                # off screen
        x0, x1 = xa + m.gap, xb - m.gap
        x1 = max(x1, x0 + 1.0)
        pal = style.selected if c.uid == active_uid else style.idle
        hw = min(m.hw, (x1 - x0) * 0.3)

        # outer body = clip, the end zones are the resize handles
        mesh.rounded_rect(x0, m.y0, x1, m.y1, min(m.radius, (x1 - x0) / 2), pal.clip)
        if hover.get("uid") == c.uid and hover.get("kind") == 'LEFT':
            mesh.rounded_rect(x0, m.y0, x0 + hw, m.y1, m.radius, style.hover_tint,
                              corners=(False, True, True, False))
        if hover.get("uid") == c.uid and hover.get("kind") == 'RIGHT':
            mesh.rounded_rect(x1 - hw, m.y0, x1, m.y1, m.radius, style.hover_tint,
                              corners=(True, False, False, True))
        if hw >= 9.0 * scale:
            mesh.chevron(x0 + hw / 2, cy, style.chevron_size * scale, -1,
                         style.chevron_width * scale, pal.chevron)
            mesh.chevron(x1 - hw / 2, cy, style.chevron_size * scale, +1,
                         style.chevron_width * scale, pal.chevron)

        # take: inset rect, purple = generated part, slate = rest, thin stripe between
        bx0, bx1 = x0 + hw, x1 - hw
        lx0, lx1 = bx0, bx1
        take = active_take(c)
        if take is not None and bx1 > bx0:
            tx0 = view.px(c.start + take.offset)
            tx1 = view.px(c.start + take.offset + take.length)
            vx0, vx1 = max(tx0, bx0), min(tx1, bx1)
            if vx1 > vx0:
                ty0, ty1 = m.y0 + m.inset, m.y1 - m.inset
                mesh.rect(vx0, ty0, vx1, ty1, pal.take_rest)
                pb = tx0 + take.progress * (tx1 - tx0)
                fx = min(max(pb, vx0), vx1)
                if fx > vx0:
                    mesh.rect(vx0, ty0, fx, ty1, pal.take_done)
                if 0.0 < take.progress < 1.0 and vx0 < pb < vx1:
                    mesh.rect(max(pb - m.stripe_w / 2, vx0), ty0,
                              min(pb + m.stripe_w / 2, vx1), ty1, pal.stripe)
                lx0, lx1 = vx0, vx1
        labels.append(((c.prompt.strip() or c.name), lx0, lx1, pal.text))

    if hover.get("kind") == 'SYNC':                 # the sync-resize dot
        a, b = by_uid.get(hover.get("uid")), by_uid.get(hover.get("other"))
        if a is not None and b is not None:
            bx = (view.px(a.end) + view.px(b.start)) / 2.0
            mesh.circle(bx, cy, m.dot_r, style.dot_ring)
            mesh.circle(bx, cy, m.dot_r - 2.0 * scale, style.dot_fill)

    gpu.state.blend_set('ALPHA')
    mesh.draw()

    font = _font_id(style)
    blf.size(font, style.font_size * scale)
    bold = getattr(blf, "BOLD", None)
    if style.bold and bold is not None:
        blf.enable(font, bold)
    ty = cy - blf.dimensions(font, "H")[1] / 2.0
    pad = style.text_padding * scale
    for text, lx0, lx1, color in labels:
        text = _fit_text(font, text, (lx1 - lx0) - 2 * pad)
        if text:
            blf.color(font, *color)
            blf.position(font, lx0 + pad, ty, 0)
            blf.draw(font, text)
    if style.bold and bold is not None:
        blf.disable(font, bold)
    gpu.state.blend_set('NONE')


def _draw_callback():
    ct = getattr(bpy.context.scene, "clip_track", None)
    if ct is not None and len(ct.clips):
        draw_track(ct.clips, STYLE, ct.active_uid, HOVER)


# ============================================================================
# 6. Operators
# ============================================================================

_MENU = {"clip": 0, "frame": 0.0}                   # what the context menu was opened on


class CLIPTRACK_OT_hover(Operator):
    """Track what the mouse is over so handles / the sync dot can light up"""
    bl_idname = "clip_track.hover"
    bl_label = "Clip Track Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _in_timeline(context)

    def invoke(self, context, event):
        ct = getattr(context.scene, "clip_track", None)
        new = ("NONE", 0, 0)
        if ct is not None and len(ct.clips):
            view, m = _view_metrics(context.region)
            hit = hit_test(list(ct.clips), view, m, STYLE,
                           event.mouse_region_x, event.mouse_region_y)
            if hit.kind in {'LEFT', 'RIGHT', 'SYNC'}:
                new = (hit.kind, hit.clip.uid, hit.other.uid if hit.other is not None else 0)
        if new != (HOVER["kind"], HOVER["uid"], HOVER["other"]):
            HOVER.update(kind=new[0], uid=new[1], other=new[2])
            context.area.tag_redraw()
        return {'PASS_THROUGH'}


class CLIPTRACK_OT_mouse(Operator):
    """Select / move / resize clips, open menus, create clips"""
    bl_idname = "clip_track.mouse"
    bl_label = "Clip Track"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _in_timeline(context)

    # -- entry ------------------------------------------------------------
    def invoke(self, context, event):
        ct = getattr(context.scene, "clip_track", None)
        if ct is None:
            return {'PASS_THROUGH'}
        view, m = _view_metrics(context.region)
        clips = list(ct.clips)
        hit = hit_test(clips, view, m, STYLE, event.mouse_region_x, event.mouse_region_y)
        if hit.kind == 'NONE':
            return {'PASS_THROUGH'}                 # not on the track: Blender's business

        select_btn = select_button(context)

        if event.type == 'W':                       # context menu key in right-click-select mode
            if select_btn != 'RIGHTMOUSE':
                return {'PASS_THROUGH'}
            return self._open_menu(hit)
        if event.type != select_btn:                # the "other" mouse button
            if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
                return self._open_menu(hit)         # left-click-select mode: RMB = menu
            return {'PASS_THROUGH'}

        if hit.kind == 'EMPTY':
            if event.value == 'DOUBLE_CLICK':
                return self._create_with_prompt(ct, clips, hit)
            if ct.active_uid:
                ct.active_uid = 0
                tag_redraw()
                return {'FINISHED'}
            return {'CANCELLED'}

        if event.value == 'DOUBLE_CLICK':
            if hit.kind == 'BODY':
                ct.active_uid = hit.clip.uid
                bpy.ops.clip_track.edit_prompt('INVOKE_DEFAULT', clip_uid=hit.clip.uid,
                                               make_take=False)
                return {'FINISHED'}
            return {'PASS_THROUGH'}                 # Blender re-sends it as a plain press
        return self._begin_drag(context, event, ct, clips, hit, view)

    def _open_menu(self, hit):
        _MENU["frame"] = hit.frame
        if hit.kind == 'EMPTY':
            _MENU["clip"] = 0
            bpy.ops.wm.call_menu(name="CLIPTRACK_MT_empty")
        else:
            _MENU["clip"] = hit.clip.uid
            bpy.ops.wm.call_menu(name="CLIPTRACK_MT_clip")
        return {'CANCELLED'}                        # consume the event, no undo step

    def _create_with_prompt(self, ct, clips, hit):
        rng = new_clip_range(clips, hit.frame, ct.default_length)
        if rng is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        clip = add_clip(ct, *rng)
        ct.active_uid = clip.uid
        tag_redraw()
        bpy.ops.clip_track.edit_prompt('INVOKE_DEFAULT', clip_uid=clip.uid, make_take=True)
        return {'FINISHED'}

    # -- drag ---------------------------------------------------------------
    def _begin_drag(self, context, event, ct, clips, hit, view):
        ct.active_uid = hit.clip.uid
        self._kind = hit.kind                       # BODY | LEFT | RIGHT | SYNC
        self._uid = hit.clip.uid
        self._other = hit.other.uid if hit.other is not None else 0
        self._snap = snapshot(clips)
        self._button = event.type
        self._mx0 = event.mouse_region_x
        self._ppf = view.ppf
        self._d = 0                                 # last *valid* offset for BODY moves
        HOVER.update(kind=hit.kind, uid=self._uid, other=self._other)
        context.window.cursor_modal_set('MOVE_X')
        context.window_manager.modal_handler_add(self)
        tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if event.type == self._button and event.value == 'RELEASE':
            return self._end(context, cancel=False)
        if event.value == 'PRESS' and (event.type == 'ESC' or (
                event.type in {'LEFTMOUSE', 'RIGHTMOUSE'} and event.type != self._button)):
            with _guard:
                clips = list(context.scene.clip_track.clips)
                restore(clips, self._snap)
            return self._end(context, cancel=True)
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}                 # keep zooming / panning usable
        return {'RUNNING_MODAL'}

    def _end(self, context, cancel):
        context.window.cursor_modal_restore()
        HOVER.update(kind="NONE", uid=0, other=0)
        tag_redraw()
        return {'CANCELLED'} if cancel else {'FINISHED'}

    def _update(self, context, event):
        clips = list(context.scene.clip_track.clips)
        by_uid = {c.uid: c for c in clips}
        clip = by_uid.get(self._uid)
        if clip is None:
            return
        d = round((event.mouse_region_x - self._mx0) / self._ppf)
        s0, e0 = self._snap[self._uid][:2]

        with _guard:
            restore(clips, self._snap)              # always recompute from the start state
            if self._kind == 'BODY':
                # Clips cannot cross: a blocked position is ignored, the clip waits
                # where it was until the mouse is over free space again.
                if is_free(clips, s0 + d, e0 + d, clip.uid):
                    self._d = d
                if self._d:
                    clip.start, clip.end = s0 + self._d, e0 + self._d   # takes keep their offsets
                    clip.bind_start = clip.bind_end = False

            elif self._kind == 'LEFT':
                ns = min(s0 + d, e0 - MIN_LEN)
                lo = left_limit(clips, clip)
                if lo is not None:
                    ns = max(ns, lo)
                if ns != s0:
                    set_start(clip, ns)
                    clip.bind_start = False

            elif self._kind == 'RIGHT':
                ne = max(e0 + d, s0 + MIN_LEN)
                hi = right_limit(clips, clip)
                if hi is not None:
                    ne = min(ne, hi)
                if ne != e0:
                    clip.end = ne
                    clip.bind_end = False

            elif self._kind == 'SYNC':              # drag the shared border of two clips
                b = by_uid.get(self._other)
                if b is not None:
                    bs0 = self._snap[b.uid][0]
                    be0 = self._snap[b.uid][1]
                    dd = max((s0 + MIN_LEN) - e0, min((be0 - MIN_LEN) - bs0, d))
                    if dd:
                        clip.end = e0 + dd
                        set_start(b, bs0 + dd)
            apply_bindings(clips)
        tag_redraw()


# --- simple operators used by menus and the panel -------------------------------------

class _ClipOp:
    """Mixin: operators that act on a clip by uid (0 = the selected clip)."""
    bl_options = {'UNDO'}
    clip_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _clip(self, context):
        ct = context.scene.clip_track
        clip = find_clip(ct, self.clip_uid) if self.clip_uid else active_clip(ct)
        if clip is None:
            self.report({'WARNING'}, "No clip")
        return ct, clip


class CLIPTRACK_OT_create(Operator):
    """Create a clip: default length, shortened to fit before the next clip"""
    bl_idname = "clip_track.create"
    bl_label = "Create Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        ct = context.scene.clip_track
        rng = new_clip_range(list(ct.clips), self.frame, ct.default_length)
        if rng is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        ct.active_uid = add_clip(ct, *rng).uid
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_fill_gap(Operator):
    """Fill the empty space under the cursor with clips of about the default length"""
    bl_idname = "clip_track.fill_gap"
    bl_label = "Fill In"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        sc = context.scene
        ct = sc.clip_track
        ranges = fill_ranges(list(ct.clips), self.frame, ct.default_length,
                             sc.frame_start, sc.frame_end)
        if not ranges:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        for s, e in ranges:
            add_clip(ct, s, e)
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_shrink_neighbors(Operator):
    """Cut the clips next to this empty space back to the current frame"""
    bl_idname = "clip_track.shrink_neighbors"
    bl_label = "Shrink Neighbors to Current Frame"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        sc = context.scene
        clips = list(sc.clip_track.clips)
        cur, f = sc.frame_current, self.frame
        prev = max((c for c in clips if c.end <= f), key=lambda c: c.end, default=None)
        nxt = min((c for c in clips if c.start > f), key=lambda c: c.start, default=None)
        changed = 0
        with _guard:
            if prev is not None and prev.start < cur < prev.end:
                prev.end = cur
                changed += 1
            if nxt is not None and nxt.start < cur < nxt.end:
                set_start(nxt, cur)
                changed += 1
            apply_bindings(clips)
        if not changed:
            self.report({'INFO'}, "The current frame is not inside a neighbouring clip")
            return {'CANCELLED'}
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_edit_prompt(_ClipOp, Operator):
    """Edit the prompt and settings of a clip"""
    bl_idname = "clip_track.edit_prompt"
    bl_label = "Clip Prompt"
    bl_options = {'UNDO'}
    prompt: StringProperty(name="Prompt", options={'SKIP_SAVE'})
    seed: IntProperty(name="Seed", min=0, options={'SKIP_SAVE'},
                      description="0 = random")
    make_take: BoolProperty(name="Generate New Take", default=True, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        self.prompt, self.seed = clip.prompt, clip.seed
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        col = self.layout.column()
        row = col.row()
        row.activate_init = True                    # start typing immediately
        row.prop(self, "prompt", text="")
        col.prop(self, "seed")
        col.prop(self, "make_take")

    def execute(self, context):
        sc = context.scene
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.prompt, clip.seed = self.prompt, self.seed
        if self.make_take:
            GENERATE(sc, clip, add_take(clip))
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_new_take(_ClipOp, Operator):
    """Generate a new take with the clip's current prompt and settings"""
    bl_idname = "clip_track.new_take"
    bl_label = "New Take"
    bl_options = {'UNDO'}

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        GENERATE(context.scene, clip, add_take(clip))
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_delete_takes(_ClipOp, Operator):
    """Delete all takes of the clip"""
    bl_idname = "clip_track.delete_takes"
    bl_label = "Delete All Takes"
    bl_options = {'UNDO'}

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.takes.clear()
        clip.active_take = 0
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_delete_clip(_ClipOp, Operator):
    """Delete the clip"""
    bl_idname = "clip_track.delete_clip"
    bl_label = "Delete Clip"
    bl_options = {'UNDO'}

    def execute(self, context):
        ct, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        remove_clip(ct, clip)
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_copy_clip(_ClipOp, Operator):
    """Copy the clip (with its takes); paste it from the menu of an empty spot"""
    bl_idname = "clip_track.copy_clip"
    bl_label = "Copy Clip"
    bl_options = set()

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        _CLIPBOARD.clear()
        _CLIPBOARD.update(_clip_to_dict(clip))
        return {'FINISHED'}


class CLIPTRACK_OT_paste_clip(Operator):
    """Paste the copied clip here (needs enough free space)"""
    bl_idname = "clip_track.paste_clip"
    bl_label = "Paste Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        ct = context.scene.clip_track
        if not _CLIPBOARD:
            return {'CANCELLED'}
        if not is_free(list(ct.clips), self.frame, self.frame + _CLIPBOARD["length"]):
            self.report({'WARNING'}, "Not enough free space for the copied clip")
            return {'CANCELLED'}
        ct.active_uid = _clip_from_dict(ct, _CLIPBOARD, self.frame).uid
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_set_take(_ClipOp, Operator):
    """Make this take the visible one"""
    bl_idname = "clip_track.set_take"
    bl_label = "Choose Take"
    bl_options = {'UNDO'}
    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.active_take = self.index
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_stick(_ClipOp, Operator):
    """Stretch the clip to its neighbours, keep it glued there, and generate a new take"""
    bl_idname = "clip_track.stick"
    bl_label = "Stick to Neighbors"
    bl_options = {'UNDO'}
    mode: EnumProperty(items=[('BOTH', "Both", ""), ('NEXT', "Next", ""),
                              ('PREV', "Previous", "")], options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        ct, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clips = list(ct.clips)
        p, n = prev_next(clips, clip)
        changed = False
        with _guard:
            if self.mode in {'PREV', 'BOTH'} and p is not None:
                set_start(clip, p.end)
                clip.bind_start = changed = True
            if self.mode in {'NEXT', 'BOTH'} and n is not None:
                clip.end = n.start
                clip.bind_end = changed = True
            apply_bindings(clips)
        if not changed:
            self.report({'INFO'}, "There is no neighbour to stick to")
            return {'CANCELLED'}
        GENERATE(context.scene, clip, add_take(clip))
        tag_redraw()
        return {'FINISHED'}


class CLIPTRACK_OT_set_frame(_ClipOp, Operator):
    """Set the clip border to the current frame"""
    bl_idname = "clip_track.set_frame"
    bl_label = "Set to Current Frame"
    bl_options = {'UNDO'}
    which: EnumProperty(items=[('START', "Start", ""), ('END', "End", "")],
                        options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        if self.which == 'START':
            clip.start = context.scene.frame_current      # clamped by the property
        else:
            clip.end = context.scene.frame_current
        tag_redraw()
        return {'FINISHED'}


# ============================================================================
# 7. Menus and sidebar panel
# ============================================================================

class CLIPTRACK_MT_empty(Menu):
    bl_label = "Clip Track"

    def draw(self, context):
        layout = self.layout
        f = math.floor(_MENU["frame"])
        layout.operator("clip_track.create", text="Create Clip", icon='ADD').frame = f
        layout.operator("clip_track.fill_gap", text="Fill In").frame = f
        layout.operator("clip_track.shrink_neighbors",
                        text="Shrink Neighbors to Current Frame").frame = f
        if _CLIPBOARD:
            layout.separator()
            layout.operator("clip_track.paste_clip", text="Paste Clip",
                            icon='PASTEDOWN').frame = f


class CLIPTRACK_MT_takes(Menu):
    bl_label = "Change Take"

    def draw(self, context):
        clip = find_clip(context.scene.clip_track, _MENU["clip"])
        if clip is None or not len(clip.takes):
            self.layout.label(text="No takes")
            return
        for i, t in enumerate(clip.takes):
            op = self.layout.operator(
                "clip_track.set_take", text=f"{t.name}   {round(t.progress * 100)}%",
                icon='CHECKMARK' if i == clip.active_take else 'BLANK1')
            op.clip_uid, op.index = clip.uid, i


class CLIPTRACK_MT_stick(Menu):
    bl_label = "Stick to Neighbors"

    def draw(self, context):
        ct = context.scene.clip_track
        clip = find_clip(ct, _MENU["clip"])
        p, n = prev_next(list(ct.clips), clip) if clip is not None else (None, None)
        has_p, has_n = p is not None, n is not None
        for text, mode, ok in (("Both", 'BOTH', has_p and has_n), ("Next", 'NEXT', has_n),
                               ("Previous", 'PREV', has_p)):
            row = self.layout.row()
            row.enabled = ok
            op = row.operator("clip_track.stick", text=text)
            op.clip_uid, op.mode = _MENU["clip"], mode


class CLIPTRACK_MT_clip(Menu):
    bl_label = "Clip"

    def draw(self, context):
        layout = self.layout
        uid = _MENU["clip"]
        layout.operator("clip_track.new_take", text="New Take").clip_uid = uid
        op = layout.operator("clip_track.edit_prompt", text="Change Prompt / Settings...")
        op.clip_uid, op.make_take = uid, True
        layout.menu("CLIPTRACK_MT_takes", text="Change Take")
        layout.menu("CLIPTRACK_MT_stick", text="Stick to Neighbors")
        layout.separator()
        layout.operator("clip_track.copy_clip", text="Copy Clip",
                        icon='COPYDOWN').clip_uid = uid
        layout.separator()
        layout.operator("clip_track.delete_takes", text="Delete All Takes").clip_uid = uid
        layout.operator("clip_track.delete_clip", text="Delete Clip",
                        icon='TRASH').clip_uid = uid


class CLIPTRACK_UL_takes(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name)
        row.label(text=f"{round(item.progress * 100)}%")


class CLIPTRACK_PT_panel(Panel):
    bl_idname = "CLIPTRACK_PT_panel"
    bl_label = "Clip Track"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clip Track"

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        ct = sc.clip_track
        layout.prop(ct, "default_length")
        layout.operator("clip_track.create", text="New Clip at Current Frame",
                        icon='ADD').frame = sc.frame_current

        clip = active_clip(ct)
        if clip is None:
            layout.label(text="Select a clip in the Timeline")
            return

        box = layout.box()
        box.prop(clip, "name")
        box.prop(clip, "prompt")
        box.prop(clip, "seed")

        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(clip, "start")
        op = row.operator("clip_track.set_frame", text="", icon='TIME')
        op.clip_uid, op.which = clip.uid, 'START'
        row.prop(clip, "bind_start", text="", toggle=True,
                 icon='LINKED' if clip.bind_start else 'UNLINKED')
        row = col.row(align=True)
        row.prop(clip, "end")
        op = row.operator("clip_track.set_frame", text="", icon='TIME')
        op.clip_uid, op.which = clip.uid, 'END'
        row.prop(clip, "bind_end", text="", toggle=True,
                 icon='LINKED' if clip.bind_end else 'UNLINKED')
        col.prop(clip, "duration")

        box = layout.box()
        box.label(text="Takes")
        box.template_list("CLIPTRACK_UL_takes", "", clip, "takes", clip, "active_take",
                          rows=3)
        take = active_take(clip)
        if take is not None:
            col = box.column(align=True)
            col.prop(take, "offset")
            col.prop(take, "length")
            col.prop(take, "progress", slider=True)
        row = box.row(align=True)
        row.operator("clip_track.new_take", text="New Take").clip_uid = clip.uid
        op = row.operator("clip_track.edit_prompt", text="Prompt...")
        op.clip_uid, op.make_take = clip.uid, True
        box.operator("clip_track.delete_takes").clip_uid = clip.uid

        layout.operator("clip_track.delete_clip", icon='TRASH').clip_uid = clip.uid


# ============================================================================
# 8. Register / unregister (safe to re-run the script from the Text Editor)
# ============================================================================

classes = (
    CT_Take, CT_Clip, CT_Scene,
    CLIPTRACK_OT_hover, CLIPTRACK_OT_mouse, CLIPTRACK_OT_create,
    CLIPTRACK_OT_fill_gap, CLIPTRACK_OT_shrink_neighbors, CLIPTRACK_OT_edit_prompt,
    CLIPTRACK_OT_new_take, CLIPTRACK_OT_delete_takes, CLIPTRACK_OT_delete_clip,
    CLIPTRACK_OT_copy_clip, CLIPTRACK_OT_paste_clip, CLIPTRACK_OT_set_take,
    CLIPTRACK_OT_stick, CLIPTRACK_OT_set_frame,
    CLIPTRACK_MT_empty, CLIPTRACK_MT_takes, CLIPTRACK_MT_stick, CLIPTRACK_MT_clip,
    CLIPTRACK_UL_takes, CLIPTRACK_PT_panel,
)

_STATE_KEY = "timeline_clip_track_state"


def _state():
    return bpy.app.driver_namespace.setdefault(
        _STATE_KEY, {"handle": None, "keymaps": [], "classes": []})


def unregister():
    st = _state()
    if st["handle"] is not None:
        bpy.types.SpaceDopeSheetEditor.draw_handler_remove(st["handle"], 'WINDOW')
        st["handle"] = None
    for km, kmi in st["keymaps"]:
        try:
            km.keymap_items.remove(kmi)
        except (RuntimeError, ReferenceError):
            pass
    st["keymaps"].clear()
    for cls in reversed(st["classes"]):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
    st["classes"].clear()
    if hasattr(bpy.types.Scene, "clip_track"):
        del bpy.types.Scene.clip_track


def register():
    unregister()                                    # also cleans up a previous script run
    st = _state()
    for cls in classes:
        bpy.utils.register_class(cls)
    st["classes"] = list(classes)
    bpy.types.Scene.clip_track = PointerProperty(type=CT_Scene)

    st["handle"] = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
        _draw_callback, (), 'WINDOW', 'POST_PIXEL')

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Dopesheet", space_type='DOPESHEET_EDITOR')
        for btn in ('LEFTMOUSE', 'RIGHTMOUSE'):
            for value in ('PRESS', 'DOUBLE_CLICK'):
                st["keymaps"].append((km, km.keymap_items.new("clip_track.mouse", btn, value)))
        st["keymaps"].append((km, km.keymap_items.new("clip_track.mouse", 'W', 'PRESS')))
        st["keymaps"].append((km, km.keymap_items.new("clip_track.hover", 'MOUSEMOVE', 'ANY')))


# ============================================================================
# Demo (Text Editor -> Run Script)
# ============================================================================

def _make_demo(ct):
    for name, start, end, progress, offset in (("Walking backward", 1, 90, 0.4, 0),
                                               ("Backflip", 90, 160, 0.75, 8),
                                               ("Resting", 160, 250, 1.0, 0)):
        clip = add_clip(ct, start, end, name=name)
        clip.prompt = name
        take = add_take(clip)
        take.progress, take.offset = progress, offset
        take.length = (end - start) - offset - (8 if offset else 0)


def launch():
    register()
    if not len(bpy.context.scene.clip_track.clips):
        _make_demo(bpy.context.scene.clip_track)
    tag_redraw()

def stop():
    unregister()

if __name__ == "__main__":
    launch()
