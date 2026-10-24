"""Timeline pixel geometry, hit testing and GPU/BLF rendering."""

import math
from dataclasses import dataclass

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from ..core import takes, tracks
from .runtime import in_timeline, runtime, ui_scale
from .track_style import STYLE


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
        self.top = (STYLE.clip_height + STYLE.y_margin) * scale
        self.bottom = self.top - STYLE.clip_height * scale
        self.handle_width = STYLE.handle_width * scale
        self.radius = STYLE.corner_radius * scale
        self.inset = STYLE.take_inset * scale
        self.gap = STYLE.clip_gap * scale
        self.dot_radius = STYLE.dot_radius * scale
        self.stripe_width = max(STYLE.stripe_min_pixels, STYLE.stripe_width * scale)


@dataclass
class HitResult:
    kind: str
    clip: object = None
    other: object = None
    frame: float = 0.0


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
    @staticmethod
    def metrics(region):
        scale = ui_scale()
        return TimelineView(region), ClipMetrics(region, scale)

    @staticmethod
    def hit_test(clips, view, metrics, mouse_x, mouse_y):
        if not (metrics.bottom <= mouse_y <= metrics.top):
            return HitResult('NONE')
        frame = view.frame(mouse_x)
        ordered_clips = tracks.ordered_clips(clips)
        for left_clip, right_clip in zip(ordered_clips, ordered_clips[1:]):
            if right_clip.start - left_clip.end <= STYLE.sync_max_gap:
                border_x = (view.pixel(left_clip.end) + view.pixel(right_clip.start)) / 2.0
                if (
                    abs(mouse_x - border_x)
                    <= metrics.dot_radius + STYLE.sync_hit_padding * metrics.scale
                ):
                    return HitResult('SYNC', left_clip, right_clip, frame)
        for clip in ordered_clips:
            left_x, right_x = view.pixel(clip.start), view.pixel(clip.end)
            if left_x <= mouse_x <= right_x:
                handle_width = min(
                    metrics.handle_width, (right_x - left_x) * STYLE.max_handle_fraction,
                )
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
        handle_width = min(metrics.handle_width, (x1 - x0) * STYLE.max_handle_fraction)
        scale = metrics.scale

        mesh.rounded_rect(x0, metrics.bottom, x1, metrics.top,
                          min(metrics.radius, (x1 - x0) / 2), palette.clip)
        if hover.uid == clip.uid and hover.kind == 'LEFT':
            mesh.rounded_rect(
                x0, metrics.bottom, x0 + handle_width, metrics.top,
                metrics.radius, STYLE.hover_tint,
                corners=(False, True, True, False),
            )
        if hover.uid == clip.uid and hover.kind == 'RIGHT':
            mesh.rounded_rect(
                x1 - handle_width, metrics.bottom, x1, metrics.top,
                metrics.radius, STYLE.hover_tint,
                corners=(True, False, False, True),
            )
        if handle_width >= STYLE.min_chevron_handle_width * scale:
            mesh.chevron(x0 + handle_width / 2, center_y, STYLE.chevron_size * scale,
                         -1, STYLE.chevron_width * scale, palette.chevron)
            mesh.chevron(x1 - handle_width / 2, center_y, STYLE.chevron_size * scale,
                         1, STYLE.chevron_width * scale, palette.chevron)

        label_left, label_right = x0 + handle_width, x1 - handle_width
        take = takes.current_take(clip)
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
        # TODO: decide if we need to show prompt here
        # labels.append((clip.prompt.strip() or clip.name, label_left, label_right, palette.text))
        labels.append((clip.name, label_left, label_right, palette.text))

    @staticmethod
    def _draw_sync_dot(mesh, clips_by_uid, view, metrics, center_y, hover):
        if hover.kind != 'SYNC':
            return
        left_clip = clips_by_uid.get(hover.uid)
        right_clip = clips_by_uid.get(hover.other_uid)
        if left_clip is None or right_clip is None:
            return
        border_x = (view.pixel(left_clip.end) + view.pixel(right_clip.start)) / 2.0
        mesh.circle(border_x, center_y, metrics.dot_radius, STYLE.dot_ring)
        mesh.circle(
            border_x, center_y,
            metrics.dot_radius - STYLE.dot_ring_width * metrics.scale, STYLE.dot_fill,
        )

    @classmethod
    def _draw_labels(cls, labels, center_y, scale):
        font_id = 0
        blf.size(font_id, STYLE.font_size * scale)
        text_y = center_y - blf.dimensions(font_id, "H")[1] / 2.0
        padding = STYLE.text_padding * scale
        for label, left_x, right_x, color in labels:
            fitted = cls._fit_text(label, (right_x - left_x) - 2 * padding)
            if fitted:
                blf.color(font_id, *color)
                blf.position(font_id, left_x + padding, text_y, 0)
                blf.draw(font_id, fitted)

    @staticmethod
    def _fit_text(text, max_width):
        font_id = 0
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
        if not in_timeline(context):
            return

        view, metrics = cls.metrics(region)
        ordered_clips = tracks.ordered_clips(clips)
        clips_by_uid = {clip.uid: clip for clip in ordered_clips}
        center_y = (metrics.bottom + metrics.top) / 2.0
        mesh = MeshBatch()
        labels = []

        for clip in ordered_clips:
            cls._draw_clip(
                mesh, labels, clip, view, metrics, center_y, active_uid, hover, region.width,
            )
        cls._draw_sync_dot(mesh, clips_by_uid, view, metrics, center_y, hover)

        previous_blend = gpu.state.blend_get()
        gpu.state.blend_set('ALPHA')
        try:
            mesh.draw()
            cls._draw_labels(labels, center_y, metrics.scale)
        finally:
            gpu.state.blend_set(previous_blend)


def draw_callback():
    clip_data = getattr(bpy.context.scene, "clip_data", None)
    if clip_data is not None and len(clip_data.clips):
        ClipDrawing.draw(clip_data.clips, clip_data.active_uid, runtime.hover)
