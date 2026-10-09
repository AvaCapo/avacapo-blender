"""
Clips and takes drawn over the Blender Timeline (Blender 5.2, gpu + blf).

Run it from the Text Editor (Alt+P) for a demo, or install it as an add-on.
Clips live in `scene.clip_data`, so they are saved with the .blend and
follow Blender's undo system.

Mouse (the "select button" follows Preferences > Keymap > Select With):
    select button   click = select, drag body = move, drag arrows = resize,
                    drag the dot between two touching clips = sync-resize,
                    double-click empty space = new clip + prompt popup,
                    double-click a clip = edit its prompt
    other button    context menu (in right-click-select mode: W, like Blender)
    Esc             cancel a drag

Hook for your backend: replace `GENERATE(scene, clip, take)` (bottom of
below). The default only animates the take's progress bar.
"""

bl_info = {
    "name": "Timeline Clips",
    "author": "",
    "version": (0, 2, 0),
    "blender": (5, 2, 0),
    "location": "Timeline / 3D Viewport > Sidebar > Clips",
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
    top_margin: float = 14.0      # gap between the ruler and the clips
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
# 2. Data (saved in the .blend, undoable)
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

    def _get_start(self):
        return self.get("_start", 0)

    def _set_start(self, value):
        if _guard.busy:
            self["_start"] = int(value)
            return
        with _guard:
            clip_data = self.id_data.clip_data
            start = min(int(value), self.get("_end", 1) - MIN_LEN)
            left_limit = clip_data.left_limit(self)
            if left_limit is not None:
                start = max(start, left_limit)
            self.set_start(start)
            clip_data.apply_bindings()

    def _get_end(self):
        return self.get("_end", 1)

    def _set_end(self, value):
        if _guard.busy:
            self["_end"] = int(value)
            return
        with _guard:
            clip_data = self.id_data.clip_data
            end = max(int(value), self.get("_start", 0) + MIN_LEN)
            right_limit = clip_data.right_limit(self)
            if right_limit is not None:
                end = min(end, right_limit)
            self.end = end
            clip_data.apply_bindings()

    def _get_duration(self):
        return self.end - self.start

    def _set_duration(self, value):
        self.end = self.start + max(MIN_LEN, int(value))

    def _on_bind_changed(self, context):
        if not _guard.busy:
            with _guard:
                self.id_data.clip_data.apply_bindings()

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

    def set_start(self, new_start):
        """Move the left border while keeping takes fixed in timeline time."""
        delta = new_start - self.start
        self.start = new_start
        for take in self.takes:
            take.offset -= delta

    def current_take(self):
        count = len(self.takes)
        return self.takes[min(max(self.active_take, 0), count - 1)] if count else None

    def add_take(self, prompt=None):
        self.take_counter += 1
        take = self.takes.add()
        take.uid = self.take_counter
        take.name = f"Take {len(self.takes)}"
        take.prompt = self.prompt if prompt is None else prompt
        take.seed = self.seed
        take.offset = 0
        take.length = max(MIN_LEN, self.end - self.start)
        take.progress = 0.0
        self.active_take = len(self.takes) - 1
        return take

    def to_dict(self):
        return {
            "name": self.name,
            "prompt": self.prompt,
            "seed": self.seed,
            "length": self.end - self.start,
            "active_take": self.active_take,
            "takes": [
                {
                    "name": take.name,
                    "prompt": take.prompt,
                    "seed": take.seed,
                    "offset": take.offset,
                    "length": take.length,
                    "progress": take.progress,
                }
                for take in self.takes
            ],
        }


class CT_Scene(PropertyGroup):
    clips: CollectionProperty(type=CT_Clip)
    active_uid: IntProperty(default=0)
    next_uid: IntProperty(default=1)
    default_length: IntProperty(
        name="Default Length", min=1, default=60,
        description="Length of a new clip when there is room for it")

    def ordered(self):
        return sorted(self.clips, key=lambda clip: (clip.start, clip.end))

    def neighbors(self, clip):
        clips = self.ordered()
        for index, candidate in enumerate(clips):
            if candidate.uid == clip.uid:
                previous = clips[index - 1] if index else None
                following = clips[index + 1] if index + 1 < len(clips) else None
                return previous, following
        return None, None

    def is_free(self, start, end, ignore_uid=0):
        return all(
            not (start < clip.end and end > clip.start)
            for clip in self.clips if clip.uid != ignore_uid
        )

    def gap_at(self, frame):
        """Return the free-space boundaries around a frame, or None if occupied."""
        if any(clip.start <= frame < clip.end for clip in self.clips):
            return None
        previous_ends = [clip.end for clip in self.clips if clip.end <= frame]
        next_starts = [clip.start for clip in self.clips if clip.start > frame]
        return (
            max(previous_ends) if previous_ends else None,
            min(next_starts) if next_starts else None,
        )

    def new_range(self, frame, default_length=None):
        frame = math.floor(frame)
        gap = self.gap_at(frame)
        if gap is None:
            return None
        if default_length is None:
            default_length = self.default_length
        end = frame + max(1, default_length)
        if gap[1] is not None:
            end = min(end, gap[1])
        return (frame, end) if end > frame else None

    def fill_ranges(self, frame, default_length, scene_start, scene_end):
        frame = math.floor(frame)
        gap = self.gap_at(frame)
        if gap is None:
            return []
        default_length = max(1, default_length)
        start = gap[0] if gap[0] is not None else min(scene_start, frame)
        end = gap[1] if gap[1] is not None else max(scene_end + 1, start + default_length)
        if end <= frame:
            start, end = frame, frame + default_length
        total = end - start
        count = max(1, min(total, round(total / default_length)))
        base_length, remainder = divmod(total, count)
        ranges = []
        cursor = start
        for index in range(count):
            next_frame = cursor + base_length + (1 if index < remainder else 0)
            ranges.append((cursor, next_frame))
            cursor = next_frame
        return ranges

    def left_limit(self, clip):
        previous, _ = self.neighbors(clip)
        if previous is None:
            return None
        return previous.start + MIN_LEN if previous.bind_end else previous.end

    def right_limit(self, clip):
        _, following = self.neighbors(clip)
        if following is None:
            return None
        return following.end - MIN_LEN if following.bind_start else following.start

    def apply_bindings(self):
        """Keep clips joined where either neighbour has a binding enabled."""
        for _ in range(len(self.clips) + 1):
            changed = False
            ordered_clips = self.ordered()
            for index, clip in enumerate(ordered_clips):
                if clip.bind_start and index > 0:
                    previous = ordered_clips[index - 1]
                    if clip.start != previous.end and previous.end < clip.end:
                        clip.set_start(previous.end)
                        changed = True
                if clip.bind_end and index + 1 < len(ordered_clips):
                    following = ordered_clips[index + 1]
                    if clip.end != following.start and following.start > clip.start:
                        clip.end = following.start
                        changed = True
            if not changed:
                break

    def snapshot(self):
        return {
            clip.uid: (
                clip.start, clip.end, clip.bind_start, clip.bind_end,
                [take.offset for take in clip.takes],
            )
            for clip in self.clips
        }

    def restore(self, snapshot):
        for clip in self.clips:
            state = snapshot.get(clip.uid)
            if state is None:
                continue
            clip.start, clip.end, clip.bind_start, clip.bind_end = state[:4]
            for take, offset in zip(clip.takes, state[4]):
                take.offset = offset

    def find(self, uid):
        return next((clip for clip in self.clips if clip.uid == uid), None)

    def active(self):
        return self.find(self.active_uid) if self.active_uid else None

    def add_clip(self, start, end, name=None):
        clip = self.clips.add()
        clip.uid = self.next_uid
        self.next_uid += 1
        clip.name = name or f"Clip {clip.uid}"
        with _guard:
            clip.start = start
            clip.end = end
        return clip

    def remove_clip(self, clip):
        previous, following = self.neighbors(clip)
        with _guard:
            if previous is not None and previous.bind_end:
                previous.bind_end = False
            if following is not None and following.bind_start:
                following.bind_start = False
        for index, candidate in enumerate(self.clips):
            if candidate.uid == clip.uid:
                self.clips.remove(index)
                break
        if self.active_uid == clip.uid:
            self.active_uid = 0

    def from_dict(self, data, start):
        clip = self.add_clip(start, start + data["length"], name=data["name"])
        clip.prompt, clip.seed = data["prompt"], data["seed"]
        for take_data in data["takes"]:
            take = clip.add_take(prompt=take_data["prompt"])
            take.name = take_data["name"]
            take.seed = take_data["seed"]
            take.offset = take_data["offset"]
            take.length = take_data["length"]
            take.progress = take_data["progress"]
        clip.active_take = data["active_take"]
        return clip


# --- take generation hook ----------------------------------------------------

def _simulate_generation(scene, clip, take):
    """Stand-in for the real backend: fills the progress bar over ~2 seconds."""
    names = (scene.name, clip.uid, take.uid)

    def tick():
        sc = bpy.data.scenes.get(names[0])
        ct = getattr(sc, "clip_data", None)
        c = ct.find(names[1]) if ct is not None else None
        t = next((t for t in c.takes if t.uid == names[2]), None) if c is not None else None
        if t is None:
            return None                                # clip/take was deleted
        t.progress = min(1.0, t.progress + 1.0)
        ClipUI.tag_redraw()
        return None if t.progress >= 1.0 else 0.1

    bpy.app.timers.register(tick, first_interval=0.5)


GENERATE = _simulate_generation    # replace with: def GENERATE(scene, clip, take): ...

_CLIPBOARD = {}


# ============================================================================
# 3. UI context and timeline coordinates
# ============================================================================

class ClipUI:
    @staticmethod
    def scale():
        system = bpy.context.preferences.system
        return getattr(system, "ui_scale", system.dpi * system.pixel_size / 72.0)

    @staticmethod
    def is_timeline(space):
        return (
            getattr(space, "ui_mode", None) == 'TIMELINE'
            or getattr(space, "mode", None) == 'TIMELINE'
        )

    @staticmethod
    def in_timeline(context):
        space, region = context.space_data, context.region
        return (
            space is not None and space.type == 'DOPESHEET_EDITOR'
            and ClipUI.is_timeline(space)
            and region is not None and region.type == 'WINDOW'
        )

    @staticmethod
    def select_button(context):
        """Return the select mouse button configured in Blender preferences."""
        keyconfig = context.window_manager.keyconfigs.active
        preferences = getattr(keyconfig, "preferences", None)
        return 'RIGHTMOUSE' if getattr(preferences, "select_mouse", 'LEFT') == 'RIGHT' else 'LEFTMOUSE'

    @staticmethod
    def tag_redraw():
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'DOPESHEET_EDITOR', 'VIEW_3D'}:
                    area.tag_redraw()


class TimelineView:
    """Convert between frames and pixels using the Timeline's View2D."""
    def __init__(self, region):
        view2d = region.view2d
        left = view2d.view_to_region(0, 0, clip=False)[0]
        right = view2d.view_to_region(10000, 0, clip=False)[0]
        self.origin_x = left
        self.pixels_per_frame = (right - left) / 10000.0 or 1e-6

    def pixel(self, frame):
        return self.origin_x + frame * self.pixels_per_frame

    def frame(self, pixel_x):
        return (pixel_x - self.origin_x) / self.pixels_per_frame


class ClipMetrics:
    def __init__(self, region, scale):
        self.scale = scale
        self.top = region.height - (STYLE.scrub_height + STYLE.top_margin) * scale
        self.bottom = self.top - STYLE.clip_height * scale
        self.handle_width = STYLE.handle_width * scale
        self.radius = STYLE.corner_radius * scale
        self.inset = STYLE.take_inset * scale
        self.gap = STYLE.clip_gap * scale
        self.dot_radius = STYLE.dot_radius * scale
        self.stripe_width = max(2.0, 3.0 * scale)


@dataclass
class HitResult:
    kind: str
    clip: object = None
    other: object = None
    frame: float = 0.0


# ============================================================================
# 4. Drawing
# ============================================================================

class MeshBatch:
    """Collect colored triangles and submit them in a single draw call."""
    def __init__(self):
        self.positions = []
        self.colors = []
        self.indices = []

    def _polygon(self, points, color):
        first = len(self.positions)
        self.positions.extend(points)
        self.colors.extend([color] * len(points))
        for index in range(1, len(points) - 1):
            self.indices.append((first, first + index, first + index + 1))

    def rect(self, x0, y0, x1, y1, color):
        self._polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], color)

    def rounded_rect(self, x0, y0, x1, y1, radius, color,
                     corners=(True, True, True, True), segments=6):
        radius = max(0.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        if radius < 0.75:
            return self.rect(x0, y0, x1, y1, color)
        arcs = (
            (x1 - radius, y1 - radius, 0, corners[0], (x1, y1)),
            (x0 + radius, y1 - radius, 90, corners[1], (x0, y1)),
            (x0 + radius, y0 + radius, 180, corners[2], (x0, y0)),
            (x1 - radius, y0 + radius, 270, corners[3], (x1, y0)),
        )
        points = []
        for center_x, center_y, angle_start, rounded, corner in arcs:
            if not rounded:
                points.append(corner)
                continue
            for segment in range(segments + 1):
                angle = math.radians(angle_start + 90.0 * segment / segments)
                points.append((
                    center_x + radius * math.cos(angle),
                    center_y + radius * math.sin(angle),
                ))
        self._polygon(points, color)

    def circle(self, center_x, center_y, radius, color, segments=20):
        self._polygon([
            (center_x + radius * math.cos(2 * math.pi * index / segments),
             center_y + radius * math.sin(2 * math.pi * index / segments))
            for index in range(segments)
        ], color)

    def line(self, point_a, point_b, thickness, color):
        dx = point_b[0] - point_a[0]
        dy = point_b[1] - point_a[1]
        length = math.hypot(dx, dy) or 1.0
        offset_x = -dy / length * thickness / 2.0
        offset_y = dx / length * thickness / 2.0
        self._polygon([
            (point_a[0] + offset_x, point_a[1] + offset_y),
            (point_b[0] + offset_x, point_b[1] + offset_y),
            (point_b[0] - offset_x, point_b[1] - offset_y),
            (point_a[0] - offset_x, point_a[1] - offset_y),
        ], color)

    def chevron(self, center_x, center_y, size, direction, thickness, color):
        tip = (center_x + direction * size * 0.5, center_y)
        back = center_x - direction * size * 0.5
        self.line((back, center_y + size), tip, thickness, color)
        self.line(tip, (back, center_y - size), thickness, color)

    def draw(self):
        if not self.indices:
            return
        shader = gpu.shader.from_builtin('FLAT_COLOR')
        batch = batch_for_shader(
            shader, 'TRIS',
            {"pos": self.positions, "color": self.colors},
            indices=self.indices,
        )
        batch.draw(shader)


class ClipDrawing:
    _font_cache = {}

    @staticmethod
    def metrics(region):
        scale = ClipUI.scale()
        return TimelineView(region), ClipMetrics(region, scale)

    @staticmethod
    def hit_test(clips, view, metrics, mouse_x, mouse_y):
        if not (metrics.bottom <= mouse_y <= metrics.top):
            return HitResult('NONE')
        frame = view.frame(mouse_x)
        ordered_clips = sorted(clips, key=lambda clip: (clip.start, clip.end))
        for left_clip, right_clip in zip(ordered_clips, ordered_clips[1:]):
            if right_clip.start - left_clip.end <= STYLE.sync_max_gap:
                border_x = (view.pixel(left_clip.end) + view.pixel(right_clip.start)) / 2.0
                if abs(mouse_x - border_x) <= metrics.dot_radius + 2.0 * metrics.scale:
                    return HitResult('SYNC', left_clip, right_clip, frame)
        for clip in ordered_clips:
            left_x, right_x = view.pixel(clip.start), view.pixel(clip.end)
            if left_x <= mouse_x <= right_x:
                handle_width = min(metrics.handle_width, (right_x - left_x) * 0.3)
                kind = (
                    'LEFT' if mouse_x <= left_x + handle_width else
                    'RIGHT' if mouse_x >= right_x - handle_width else 'BODY'
                )
                return HitResult(kind, clip, None, frame)
        return HitResult('EMPTY', None, None, frame)

    @staticmethod
    def _draw_clip(mesh, labels, clip, view, metrics, center_y, active_uid, hover, region_width):
        left_edge, right_edge = view.pixel(clip.start), view.pixel(clip.end)
        if right_edge < -metrics.handle_width or left_edge > region_width + metrics.handle_width:
            return

        x0, x1 = left_edge + metrics.gap, right_edge - metrics.gap
        x1 = max(x1, x0 + 1.0)
        palette = STYLE.selected if clip.uid == active_uid else STYLE.idle
        handle_width = min(metrics.handle_width, (x1 - x0) * 0.3)
        scale = metrics.scale

        mesh.rounded_rect(x0, metrics.bottom, x1, metrics.top,
                          min(metrics.radius, (x1 - x0) / 2), palette.clip)
        if hover.get("uid") == clip.uid and hover.get("kind") == 'LEFT':
            mesh.rounded_rect(
                x0, metrics.bottom, x0 + handle_width, metrics.top,
                metrics.radius, STYLE.hover_tint,
                corners=(False, True, True, False),
            )
        if hover.get("uid") == clip.uid and hover.get("kind") == 'RIGHT':
            mesh.rounded_rect(
                x1 - handle_width, metrics.bottom, x1, metrics.top,
                metrics.radius, STYLE.hover_tint,
                corners=(True, False, False, True),
            )
        if handle_width >= 9.0 * scale:
            mesh.chevron(x0 + handle_width / 2, center_y, STYLE.chevron_size * scale,
                         -1, STYLE.chevron_width * scale, palette.chevron)
            mesh.chevron(x1 - handle_width / 2, center_y, STYLE.chevron_size * scale,
                         1, STYLE.chevron_width * scale, palette.chevron)

        label_left, label_right = x0 + handle_width, x1 - handle_width
        take = clip.current_take()
        if take is not None and label_right > label_left:
            take_start = view.pixel(clip.start + take.offset)
            take_end = view.pixel(clip.start + take.offset + take.length)
            visible_start = max(take_start, label_left)
            visible_end = min(take_end, label_right)
            if visible_end > visible_start:
                take_top = metrics.bottom + metrics.inset
                take_bottom = metrics.top - metrics.inset
                mesh.rect(visible_start, take_top, visible_end, take_bottom, palette.take_rest)
                progress_x = take_start + take.progress * (take_end - take_start)
                fill_end = min(max(progress_x, visible_start), visible_end)
                if fill_end > visible_start:
                    mesh.rect(visible_start, take_top, fill_end, take_bottom, palette.take_done)
                if 0.0 < take.progress < 1.0 and visible_start < progress_x < visible_end:
                    mesh.rect(
                        max(progress_x - metrics.stripe_width / 2, visible_start), take_top,
                        min(progress_x + metrics.stripe_width / 2, visible_end), take_bottom,
                        palette.stripe,
                    )
                label_left, label_right = visible_start, visible_end

        labels.append((clip.prompt.strip() or clip.name, label_left, label_right, palette.text))

    @staticmethod
    def _draw_sync_dot(mesh, clips_by_uid, view, metrics, center_y, hover):
        if hover.get("kind") != 'SYNC':
            return
        left_clip = clips_by_uid.get(hover.get("uid"))
        right_clip = clips_by_uid.get(hover.get("other"))
        if left_clip is None or right_clip is None:
            return
        border_x = (view.pixel(left_clip.end) + view.pixel(right_clip.start)) / 2.0
        mesh.circle(border_x, center_y, metrics.dot_radius, STYLE.dot_ring)
        mesh.circle(border_x, center_y, metrics.dot_radius - 2.0 * metrics.scale, STYLE.dot_fill)

    @classmethod
    def _draw_labels(cls, labels, center_y, scale):
        font_id = cls._font_id()
        blf.size(font_id, STYLE.font_size * scale)
        bold_flag = getattr(blf, "BOLD", None)
        use_bold = STYLE.bold and bold_flag is not None
        if use_bold:
            blf.enable(font_id, bold_flag)
        text_y = center_y - blf.dimensions(font_id, "H")[1] / 2.0
        padding = STYLE.text_padding * scale
        for label, left_x, right_x, color in labels:
            fitted = cls._fit_text(font_id, label, (right_x - left_x) - 2 * padding)
            if fitted:
                blf.color(font_id, *color)
                blf.position(font_id, left_x + padding, text_y, 0)
                blf.draw(font_id, fitted)
        if use_bold:
            blf.disable(font_id, bold_flag)

    @classmethod
    def _font_id(cls):
        if not STYLE.font_path:
            return 0
        if STYLE.font_path not in cls._font_cache:
            font_id = blf.load(STYLE.font_path) if os.path.exists(STYLE.font_path) else -1
            cls._font_cache[STYLE.font_path] = font_id if font_id != -1 else 0
        return cls._font_cache[STYLE.font_path]

    @staticmethod
    def _fit_text(font_id, text, max_width):
        if max_width <= 0:
            return ""
        if blf.dimensions(font_id, text)[0] <= max_width:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if blf.dimensions(font_id, text[:middle] + "\u2026")[0] <= max_width:
                low = middle
            else:
                high = middle - 1
        return text[:low] + "\u2026" if low else ""

    @classmethod
    def draw(cls, clips, active_uid, hover):
        context = bpy.context
        space, region = context.space_data, context.region
        if space is None or region is None or not ClipUI.is_timeline(space):
            return

        view, metrics = cls.metrics(region)
        ordered_clips = sorted(clips, key=lambda clip: (clip.start, clip.end))
        clips_by_uid = {clip.uid: clip for clip in ordered_clips}
        center_y = (metrics.bottom + metrics.top) / 2.0
        mesh = MeshBatch()
        labels = []

        for clip in ordered_clips:
            cls._draw_clip(
                mesh, labels, clip, view, metrics, center_y, active_uid, hover, region.width,
            )
        cls._draw_sync_dot(mesh, clips_by_uid, view, metrics, center_y, hover)

        gpu.state.blend_set('ALPHA')
        mesh.draw()
        cls._draw_labels(labels, center_y, metrics.scale)
        gpu.state.blend_set('NONE')


def _draw_callback():
    clip_data = getattr(bpy.context.scene, "clip_data", None)
    if clip_data is not None and len(clip_data.clips):
        ClipDrawing.draw(clip_data.clips, clip_data.active_uid, HOVER)


# ============================================================================
# 5. Operators
# ============================================================================

_MENU = {"clip": 0, "frame": 0.0}                   # what the context menu was opened on


class CLIPS_OT_hover(Operator):
    """Detect which clip handle or sync dot is under the mouse"""
    bl_idname = "clips.hover"
    bl_label = "Clips Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return ClipUI.in_timeline(context)

    def invoke(self, context, event):
        clip_data = getattr(context.scene, "clip_data", None)
        new = ("NONE", 0, 0)
        if clip_data is not None and len(clip_data.clips):
            view, m = ClipDrawing.metrics(context.region)
            hit = ClipDrawing.hit_test(list(clip_data.clips), view, m,
                           event.mouse_region_x, event.mouse_region_y)
            if hit.kind in {'LEFT', 'RIGHT', 'SYNC'}:
                new = (hit.kind, hit.clip.uid, hit.other.uid if hit.other is not None else 0)
        if new != (HOVER["kind"], HOVER["uid"], HOVER["other"]):
            HOVER.update(kind=new[0], uid=new[1], other=new[2])
            context.area.tag_redraw()
        return {'PASS_THROUGH'}


class CLIPS_OT_mouse(Operator):
    """Select / move / resize clips, open menus, create clips"""
    bl_idname = "clips.mouse"
    bl_label = "Clips"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return ClipUI.in_timeline(context)

    # -- entry ------------------------------------------------------------
    def invoke(self, context, event):
        ct = getattr(context.scene, "clip_data", None)
        if ct is None:
            return {'PASS_THROUGH'}
        view, m = ClipDrawing.metrics(context.region)
        clips = list(ct.clips)
        hit = ClipDrawing.hit_test(clips, view, m, event.mouse_region_x, event.mouse_region_y)
        if hit.kind == 'NONE':
            return {'PASS_THROUGH'}                 # outside the clip area: Blender handles it

        select_btn = ClipUI.select_button(context)

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
                ClipUI.tag_redraw()
                return {'FINISHED'}
            return {'CANCELLED'}

        if event.value == 'DOUBLE_CLICK':
            if hit.kind == 'BODY':
                ct.active_uid = hit.clip.uid
                bpy.ops.clips.edit_prompt('INVOKE_DEFAULT', clip_uid=hit.clip.uid,
                                               make_take=False)
                return {'FINISHED'}
            return {'PASS_THROUGH'}                 # Blender re-sends it as a plain press
        return self._begin_drag(context, event, ct, hit, view)

    def _open_menu(self, hit):
        _MENU["frame"] = hit.frame
        if hit.kind == 'EMPTY':
            _MENU["clip"] = 0
            bpy.ops.wm.call_menu(name="CLIPS_MT_empty")
        else:
            _MENU["clip"] = hit.clip.uid
            bpy.ops.wm.call_menu(name="CLIPS_MT_clip")
        return {'CANCELLED'}                        # consume the event, no undo step

    def _create_with_prompt(self, ct, clips, hit):
        rng = ct.new_range(hit.frame, ct.default_length)
        if rng is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        clip = ct.add_clip(*rng)
        ct.active_uid = clip.uid
        ClipUI.tag_redraw()
        bpy.ops.clips.edit_prompt('INVOKE_DEFAULT', clip_uid=clip.uid, make_take=True)
        return {'FINISHED'}

    # -- drag ---------------------------------------------------------------
    def _begin_drag(self, context, event, clip_data, hit, view):
        clip_data.active_uid = hit.clip.uid
        self._drag_kind = hit.kind
        self._clip_uid = hit.clip.uid
        self._other_clip_uid = hit.other.uid if hit.other is not None else 0
        self._initial_clip_state = clip_data.snapshot()
        self._mouse_button = event.type
        self._start_mouse_x = event.mouse_region_x
        self._pixels_per_frame = view.pixels_per_frame
        self._last_valid_delta = 0
        HOVER.update(kind=hit.kind, uid=self._clip_uid, other=self._other_clip_uid)
        context.window.cursor_modal_set('MOVE_X')
        context.window_manager.modal_handler_add(self)
        ClipUI.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if event.type == self._mouse_button and event.value == 'RELEASE':
            return self._end(context, cancel=False)
        if event.value == 'PRESS' and (event.type == 'ESC' or (
                event.type in {'LEFTMOUSE', 'RIGHTMOUSE'} and event.type != self._mouse_button)):
            with _guard:
                context.scene.clip_data.restore(self._initial_clip_state)
            return self._end(context, cancel=True)
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}

    def _end(self, context, cancel):
        context.window.cursor_modal_restore()
        HOVER.update(kind="NONE", uid=0, other=0)
        ClipUI.tag_redraw()
        return {'CANCELLED'} if cancel else {'FINISHED'}

    def _update(self, context, event):
        clip_data = context.scene.clip_data
        clips = list(clip_data.clips)
        clips_by_uid = {clip.uid: clip for clip in clips}
        clip = clips_by_uid.get(self._clip_uid)
        if clip is None:
            return

        frame_delta = round((event.mouse_region_x - self._start_mouse_x) / self._pixels_per_frame)
        original_start, original_end = self._initial_clip_state[self._clip_uid][:2]

        with _guard:
            clip_data.restore(self._initial_clip_state)
            if self._drag_kind == 'BODY':
                if clip_data.is_free(
                    original_start + frame_delta, original_end + frame_delta, clip.uid,
                ):
                    self._last_valid_delta = frame_delta
                if self._last_valid_delta:
                    clip.start = original_start + self._last_valid_delta
                    clip.end = original_end + self._last_valid_delta
                    clip.bind_start = clip.bind_end = False

            elif self._drag_kind == 'LEFT':
                new_start = min(original_start + frame_delta, original_end - MIN_LEN)
                left_limit = clip_data.left_limit(clip)
                if left_limit is not None:
                    new_start = max(new_start, left_limit)
                if new_start != original_start:
                    clip.set_start(new_start)
                    clip.bind_start = False

            elif self._drag_kind == 'RIGHT':
                new_end = max(original_end + frame_delta, original_start + MIN_LEN)
                right_limit = clip_data.right_limit(clip)
                if right_limit is not None:
                    new_end = min(new_end, right_limit)
                if new_end != original_end:
                    clip.end = new_end
                    clip.bind_end = False

            elif self._drag_kind == 'SYNC':
                other_clip = clips_by_uid.get(self._other_clip_uid)
                if other_clip is not None:
                    other_start, other_end = self._initial_clip_state[other_clip.uid][:2]
                    delta = max(
                        original_start + MIN_LEN - original_end,
                        min(other_end - MIN_LEN - other_start, frame_delta),
                    )
                    if delta:
                        clip.end = original_end + delta
                        other_clip.set_start(other_start + delta)

            clip_data.apply_bindings()
        ClipUI.tag_redraw()


# --- simple operators used by menus and the panel -------------------------------------

class _ClipOp:
    """Mixin: operators that act on a clip by uid (0 = the selected clip)."""
    bl_options = {'UNDO'}
    clip_uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _clip(self, context):
        ct = context.scene.clip_data
        clip = ct.find(self.clip_uid) if self.clip_uid else ct.active()
        if clip is None:
            self.report({'WARNING'}, "No clip")
        return ct, clip


class CLIPS_OT_create(Operator):
    """Create a clip: default length, shortened to fit before the next clip"""
    bl_idname = "clips.create"
    bl_label = "Create Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        ct = context.scene.clip_data
        rng = ct.new_range(self.frame, ct.default_length)
        if rng is None:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        ct.active_uid = ct.add_clip(*rng).uid
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_fill_gap(Operator):
    """Fill the empty space under the cursor with clips of about the default length"""
    bl_idname = "clips.fill_gap"
    bl_label = "Fill In"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        clip_data = scene.clip_data
        ranges = clip_data.fill_ranges(
            self.frame, clip_data.default_length, scene.frame_start, scene.frame_end,
        )
        if not ranges:
            self.report({'WARNING'}, "No free space here")
            return {'CANCELLED'}
        for start, end in ranges:
            clip_data.add_clip(start, end)
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_shrink_neighbors(Operator):
    """Cut the clips next to this empty space back to the current frame"""
    bl_idname = "clips.shrink_neighbors"
    bl_label = "Shrink Neighbors to Current Frame"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        clip_data = scene.clip_data
        clips = list(clip_data.clips)
        current_frame, frame = scene.frame_current, self.frame
        previous = max((clip for clip in clips if clip.end <= frame),
                       key=lambda clip: clip.end, default=None)
        following = min((clip for clip in clips if clip.start > frame),
                        key=lambda clip: clip.start, default=None)
        changed = 0
        with _guard:
            if previous is not None and previous.start < current_frame < previous.end:
                previous.end = current_frame
                changed += 1
            if following is not None and following.start < current_frame < following.end:
                following.set_start(current_frame)
                changed += 1
            clip_data.apply_bindings()
        if not changed:
            self.report({'INFO'}, "The current frame is not inside a neighbouring clip")
            return {'CANCELLED'}
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_edit_prompt(_ClipOp, Operator):
    """Edit the prompt and settings of a clip"""
    bl_idname = "clips.edit_prompt"
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
            GENERATE(sc, clip, clip.add_take())
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_new_take(_ClipOp, Operator):
    """Generate a new take with the clip's current prompt and settings"""
    bl_idname = "clips.new_take"
    bl_label = "New Take"
    bl_options = {'UNDO'}

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        GENERATE(context.scene, clip, clip.add_take())
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_takes(_ClipOp, Operator):
    """Delete all takes of the clip"""
    bl_idname = "clips.delete_takes"
    bl_label = "Delete All Takes"
    bl_options = {'UNDO'}

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.takes.clear()
        clip.active_take = 0
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_delete_clip(_ClipOp, Operator):
    """Delete the clip"""
    bl_idname = "clips.delete_clip"
    bl_label = "Delete Clip"
    bl_options = {'UNDO'}

    def execute(self, context):
        ct, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        ct.remove_clip(clip)
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_copy_clip(_ClipOp, Operator):
    """Copy the clip (with its takes); paste it from the menu of an empty spot"""
    bl_idname = "clips.copy_clip"
    bl_label = "Copy Clip"
    bl_options = set()

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        _CLIPBOARD.clear()
        _CLIPBOARD.update(clip.to_dict())
        return {'FINISHED'}


class CLIPS_OT_paste_clip(Operator):
    """Paste the copied clip here (needs enough free space)"""
    bl_idname = "clips.paste_clip"
    bl_label = "Paste Clip"
    bl_options = {'UNDO'}
    frame: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        ct = context.scene.clip_data
        if not _CLIPBOARD:
            return {'CANCELLED'}
        if not ct.is_free(self.frame, self.frame + _CLIPBOARD["length"]):
            self.report({'WARNING'}, "Not enough free space for the copied clip")
            return {'CANCELLED'}
        ct.active_uid = ct.from_dict(_CLIPBOARD, self.frame).uid
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_set_take(_ClipOp, Operator):
    """Make this take the visible one"""
    bl_idname = "clips.set_take"
    bl_label = "Choose Take"
    bl_options = {'UNDO'}
    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        _, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clip.active_take = self.index
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_stick(_ClipOp, Operator):
    """Stretch the clip to its neighbours, keep it glued there, and generate a new take"""
    bl_idname = "clips.stick"
    bl_label = "Stick to Neighbors"
    bl_options = {'UNDO'}
    mode: EnumProperty(items=[('BOTH', "Both", ""), ('NEXT', "Next", ""),
                              ('PREV', "Previous", "")], options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        ct, clip = self._clip(context)
        if clip is None:
            return {'CANCELLED'}
        clips = list(ct.clips)
        p, n = ct.neighbors(clip)
        changed = False
        with _guard:
            if self.mode in {'PREV', 'BOTH'} and p is not None:
                clip.set_start(p.end)
                clip.bind_start = changed = True
            if self.mode in {'NEXT', 'BOTH'} and n is not None:
                clip.end = n.start
                clip.bind_end = changed = True
            context.scene.clip_data.apply_bindings()
        if not changed:
            self.report({'INFO'}, "There is no neighbour to stick to")
            return {'CANCELLED'}
        GENERATE(context.scene, clip, clip.add_take())
        ClipUI.tag_redraw()
        return {'FINISHED'}


class CLIPS_OT_set_frame(_ClipOp, Operator):
    """Set the clip border to the current frame"""
    bl_idname = "clips.set_frame"
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
        ClipUI.tag_redraw()
        return {'FINISHED'}


# ============================================================================
# 6. Menus and sidebar panel
# ============================================================================

class CLIPS_MT_empty(Menu):
    bl_label = "Clips"

    def draw(self, context):
        layout = self.layout
        f = math.floor(_MENU["frame"])
        layout.operator("clips.create", text="Create Clip", icon='ADD').frame = f
        layout.operator("clips.fill_gap", text="Fill In").frame = f
        layout.operator("clips.shrink_neighbors",
                        text="Shrink Neighbors to Current Frame").frame = f
        if _CLIPBOARD:
            layout.separator()
            layout.operator("clips.paste_clip", text="Paste Clip",
                            icon='PASTEDOWN').frame = f


class CLIPS_MT_takes(Menu):
    bl_label = "Change Take"

    def draw(self, context):
        clip = context.scene.clip_data.find(_MENU["clip"])
        if clip is None or not len(clip.takes):
            self.layout.label(text="No takes")
            return
        for i, t in enumerate(clip.takes):
            op = self.layout.operator(
                "clips.set_take", text=f"{t.name}   {round(t.progress * 100)}%",
                icon='CHECKMARK' if i == clip.active_take else 'BLANK1')
            op.clip_uid, op.index = clip.uid, i


class CLIPS_MT_stick(Menu):
    bl_label = "Stick to Neighbors"

    def draw(self, context):
        ct = context.scene.clip_data
        clip = ct.find(_MENU["clip"])
        p, n = ct.neighbors(clip) if clip is not None else (None, None)
        has_p, has_n = p is not None, n is not None
        for text, mode, ok in (("Both", 'BOTH', has_p and has_n), ("Next", 'NEXT', has_n),
                               ("Previous", 'PREV', has_p)):
            row = self.layout.row()
            row.enabled = ok
            op = row.operator("clips.stick", text=text)
            op.clip_uid, op.mode = _MENU["clip"], mode


class CLIPS_MT_clip(Menu):
    bl_label = "Clip"

    def draw(self, context):
        layout = self.layout
        uid = _MENU["clip"]
        layout.operator("clips.new_take", text="New Take").clip_uid = uid
        op = layout.operator("clips.edit_prompt", text="Change Prompt / Settings...")
        op.clip_uid, op.make_take = uid, True
        layout.menu("CLIPS_MT_takes", text="Change Take")
        layout.menu("CLIPS_MT_stick", text="Stick to Neighbors")
        layout.separator()
        layout.operator("clips.copy_clip", text="Copy Clip",
                        icon='COPYDOWN').clip_uid = uid
        layout.separator()
        layout.operator("clips.delete_takes", text="Delete All Takes").clip_uid = uid
        layout.operator("clips.delete_clip", text="Delete Clip",
                        icon='TRASH').clip_uid = uid


class CLIPS_UL_takes(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name)
        row.label(text=f"{round(item.progress * 100)}%")


class CLIPS_PT_panel(Panel):
    bl_idname = "CLIPS_PT_panel"
    bl_label = "Clips"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clips"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        clip_data = scene.clip_data
        layout.prop(clip_data, "default_length")
        layout.operator("clips.create", text="New Clip at Current Frame",
                        icon='ADD').frame = scene.frame_current

        clip = clip_data.active()
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
        op = row.operator("clips.set_frame", text="", icon='TIME')
        op.clip_uid, op.which = clip.uid, 'START'
        row.prop(clip, "bind_start", text="", toggle=True,
                 icon='LINKED' if clip.bind_start else 'UNLINKED')
        row = col.row(align=True)
        row.prop(clip, "end")
        op = row.operator("clips.set_frame", text="", icon='TIME')
        op.clip_uid, op.which = clip.uid, 'END'
        row.prop(clip, "bind_end", text="", toggle=True,
                 icon='LINKED' if clip.bind_end else 'UNLINKED')
        col.prop(clip, "duration")

        box = layout.box()
        box.label(text="Takes")
        box.template_list("CLIPS_UL_takes", "", clip, "takes", clip, "active_take",
                          rows=3)
        take = clip.current_take()
        if take is not None:
            col = box.column(align=True)
            col.prop(take, "offset")
            col.prop(take, "length")
            col.prop(take, "progress", slider=True)
        row = box.row(align=True)
        row.operator("clips.new_take", text="New Take").clip_uid = clip.uid
        op = row.operator("clips.edit_prompt", text="Prompt...")
        op.clip_uid, op.make_take = clip.uid, True
        box.operator("clips.delete_takes").clip_uid = clip.uid

        layout.operator("clips.delete_clip", icon='TRASH').clip_uid = clip.uid


# ============================================================================
# 7. Register / unregister (safe to re-run the script from the Text Editor)
# ============================================================================

classes = (
    CT_Take, CT_Clip, CT_Scene,
    CLIPS_OT_hover, CLIPS_OT_mouse, CLIPS_OT_create,
    CLIPS_OT_fill_gap, CLIPS_OT_shrink_neighbors, CLIPS_OT_edit_prompt,
    CLIPS_OT_new_take, CLIPS_OT_delete_takes, CLIPS_OT_delete_clip,
    CLIPS_OT_copy_clip, CLIPS_OT_paste_clip, CLIPS_OT_set_take,
    CLIPS_OT_stick, CLIPS_OT_set_frame,
    CLIPS_MT_empty, CLIPS_MT_takes, CLIPS_MT_stick, CLIPS_MT_clip,
    CLIPS_UL_takes, CLIPS_PT_panel,
)

_STATE_KEY = "timeline_clips_state"
_LEGACY_STATE_KEY = "timeline_clip_track_state"


def _state():
    return bpy.app.driver_namespace.setdefault(
        _STATE_KEY, {"handle": None, "keymaps": [], "classes": []})


def unregister():
    for state_key in (_STATE_KEY, _LEGACY_STATE_KEY):
        state = bpy.app.driver_namespace.get(state_key)
        if state is None:
            continue
        if state["handle"] is not None:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(state["handle"], 'WINDOW')
            state["handle"] = None
        for keymap, keymap_item in state["keymaps"]:
            try:
                keymap.keymap_items.remove(keymap_item)
            except (RuntimeError, ReferenceError):
                pass
        state["keymaps"].clear()
        for registered_class in reversed(state["classes"]):
            try:
                bpy.utils.unregister_class(registered_class)
            except (RuntimeError, ValueError):
                pass
        state["classes"].clear()
    for property_name in ("clip_data", "clip_track"):
        if hasattr(bpy.types.Scene, property_name):
            delattr(bpy.types.Scene, property_name)


def register():
    unregister()                                    # also cleans up a previous script run
    st = _state()
    for cls in classes:
        bpy.utils.register_class(cls)
    st["classes"] = list(classes)
    bpy.types.Scene.clip_data = PointerProperty(type=CT_Scene)

    st["handle"] = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
        _draw_callback, (), 'WINDOW', 'POST_PIXEL')

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Dopesheet", space_type='DOPESHEET_EDITOR')
        for btn in ('LEFTMOUSE', 'RIGHTMOUSE'):
            for value in ('PRESS', 'DOUBLE_CLICK'):
                st["keymaps"].append((km, km.keymap_items.new("clips.mouse", btn, value)))
        st["keymaps"].append((km, km.keymap_items.new("clips.mouse", 'W', 'PRESS')))
        st["keymaps"].append((km, km.keymap_items.new("clips.hover", 'MOUSEMOVE', 'ANY')))


# ============================================================================
# Demo (Text Editor -> Run Script)
# ============================================================================

def _make_demo(clip_data):
    for name, start, end, progress, offset in (("Walking backward", 1, 90, 0.4, 0),
                                               ("Backflip", 90, 160, 0.75, 8),
                                               ("Resting", 160, 250, 1.0, 0)):
        clip = clip_data.add_clip(start, end, name=name)
        clip.prompt = name
        take = clip.add_take()
        take.progress, take.offset = progress, offset
        take.length = (end - start) - offset - (8 if offset else 0)


def launch():
    register()
    if not len(bpy.context.scene.clip_data.clips):
        _make_demo(bpy.context.scene.clip_data)
    ClipUI.tag_redraw()

def stop():
    unregister()

if __name__ == "__main__":
    launch()
