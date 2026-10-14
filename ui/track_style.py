"""Timeline appearance in UI units; no Blender API required."""

from dataclasses import dataclass

@dataclass(frozen=True)
class Palette:
    clip: tuple[float, ...]        # outer rounded body + handles
    take_done: tuple[float, ...]   # generated part of the take
    take_rest: tuple[float, ...]   # not generated yet
    stripe: tuple[float, ...]      # thin line between the two
    text: tuple[float, ...]
    chevron: tuple[float, ...]


@dataclass
class Style:
    clip_height: float = 36.0
    y_margin: float = 14.0      # gap between the ruler and the clips
    handle_width: float = 15.0
    take_inset: float = 3.0       # take rect inset from the clip top/bottom
    corner_radius: float = 5.0
    clip_gap: float = 0.8         # visual gap on each side of a border
    chevron_size: float = 5.0
    chevron_width: float = 2.0
    dot_radius: float = 5.0
    sync_max_gap: int = 0         # frames between clips that still get a sync dot

    font_size: float = 14.0
    bold: bool = True
    text_padding: float = 10.0

    prompt_dialog_width: int = 440
    take_list_rows: int = 3
    max_handle_fraction: float = 0.3
    sync_hit_padding: float = 2.0
    dot_ring_width: float = 2.0
    stripe_min_pixels: float = 2.0
    stripe_width: float = 3.0
    min_chevron_handle_width: float = 9.0

    idle: Palette = Palette(
        clip=(0.47, 0.41, 0.49, 0.7), take_done=(0.38, 0.24, 0.40, 0.8),
        take_rest=(0.29, 0.31, 0.37, 1.0), stripe=(0.26, 0.31, 0.50, 0.8),
        text=(0.66, 0.57, 0.68, 0.8), chevron=(0.14, 0.13, 0.27, 0.3))
    selected: Palette = Palette(
        clip=(0.63, 0.55, 0.64, 0.8), take_done=(0.49, 0.29, 0.51, 0.9),
        take_rest=(0.33, 0.35, 0.42, 0.9), stripe=(0.30, 0.36, 0.58, 0.9),
        text=(0.76, 0.67, 0.78, 1.0), chevron=(0.14, 0.13, 0.27, 0.4))
    hover_tint: tuple[float, ...] = (1.0, 1.0, 1.0, 0.16)
    dot_ring: tuple[float, ...] = (0.86, 0.83, 0.90, 0.7)
    dot_fill: tuple[float, ...] = (0.42, 0.42, 0.45, 0.7)


STYLE = Style()
