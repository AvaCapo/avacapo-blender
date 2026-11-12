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
    clip_height: float = 30.0
    y_margin: float = 13.0
    handle_width: float = 18.0
    take_inset: float = 4.0
    corner_radius: float = 4.0
    clip_gap: float = 0.8
    chevron_size: float = 4.5
    chevron_width: float = 2.0
    dot_radius: float = 6.0
    sync_max_gap: int = 0         # frames between clips that still get a sync dot

    font_size: float = 13.0
    text_padding: float = 7.0

    prompt_dialog_width: int = 440
    max_handle_fraction: float = 0.3
    sync_hit_padding: float = 2.0
    dot_ring_width: float = 2.0
    stripe_min_pixels: float = 2.0
    stripe_width: float = 3.0
    min_chevron_handle_width: float = 9.0
    track_padding: float = 5.0

    tooltip_delay: float = 0.5
    hover_timer_interval: float = 0.1
    tooltip_font_size: float = 12.0
    tooltip_line_height: float = 17.0
    tooltip_padding: float = 8.0
    tooltip_offset: float = 12.0
    tooltip_max_width: float = 440.0

    track_background: tuple[float, ...] = (0.12, 0.12, 0.12, 1.0)
    track_border: tuple[float, ...] = (0.22, 0.22, 0.22, 1.0)

    idle: Palette = Palette(
        clip=(0.335, 0.310, 0.355, 1.0),
        take_done=(0.275, 0.275, 0.340, 1.0),
        take_rest=(0.215, 0.205, 0.230, 1.0),
        stripe=(0.525, 0.550, 0.705, 1.0),
        text=(0.80, 0.80, 0.80, 1.0),
        chevron=(0.165, 0.150, 0.185, 1.0),
    )
    selected: Palette = Palette(
        clip=(0.505, 0.475, 0.540, 1.0),
        take_done=(0.390, 0.380, 0.475, 1.0),
        take_rest=(0.340, 0.320, 0.360, 1.0),
        stripe=(0.680, 0.705, 0.860, 1.0),
        text=(0.96, 0.96, 0.96, 1.0),
        chevron=(0.205, 0.190, 0.230, 1.0),
    )
    warning_idle: Palette = Palette(
        clip=(0.48, 0.25, 0.23, 1.0),
        take_done=(0.38, 0.21, 0.20, 1.0),
        take_rest=(0.29, 0.18, 0.17, 1.0),
        stripe=(0.72, 0.43, 0.38, 1.0),
        text=(0.90, 0.79, 0.77, 1.0),
        chevron=(0.22, 0.12, 0.11, 1.0),
    )
    warning_selected: Palette = Palette(
        clip=(0.66, 0.36, 0.32, 1.0),
        take_done=(0.54, 0.30, 0.27, 1.0),
        take_rest=(0.43, 0.25, 0.23, 1.0),
        stripe=(0.88, 0.57, 0.49, 1.0),
        text=(1.0, 0.93, 0.90, 1.0),
        chevron=(0.29, 0.15, 0.13, 1.0),
    )
    hover_tint: tuple[float, ...] = (1.0, 1.0, 1.0, 0.40)
    dot_ring: tuple[float, ...] = (0.795, 0.770, 0.820, 0.7)
    dot_fill: tuple[float, ...] = (0.315, 0.290, 0.340, 1.0)
    tooltip_background: tuple[float, ...] = (0.08, 0.08, 0.08, 1.0)
    tooltip_border: tuple[float, ...] = (0.28, 0.28, 0.28, 1.0)
    tooltip_text: tuple[float, ...] = (0.90, 0.90, 0.90, 1.0)
    tooltip_warning: tuple[float, ...] = (1.0, 0.60, 0.53, 1.0)


STYLE = Style()
