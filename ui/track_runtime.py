"""Blender context helpers and temporary timeline UI state."""

from dataclasses import dataclass, field

import bpy

from ..core.schemas import ClipData


@dataclass(frozen=True)
class HoverTarget:
    kind: str = "NONE"
    uid: int = 0
    other_uid: int = 0


@dataclass(frozen=True)
class MenuTarget:
    frame: float = 0.0
    clip_uid: int = 0


@dataclass
class TimelineRuntimeState:
    hover: HoverTarget = field(default_factory=HoverTarget)
    menu_target: MenuTarget = field(default_factory=MenuTarget)
    clipboard: ClipData | None = None

    def reset(self):
        self.hover = HoverTarget()
        self.menu_target = MenuTarget()
        self.clipboard = None

def ui_scale():
    system = bpy.context.preferences.system
    return getattr(system, "ui_scale", system.dpi * system.pixel_size / 72.0)


def in_timeline(context):
    space = context.space_data
    region = context.region
    if space is None:
        return False
    if space.type != 'DOPESHEET_EDITOR':
        return False
    mode = getattr(space, "mode", None)
    if mode != 'TIMELINE':
        return False
    if region is None:
        return False
    if region.type != 'WINDOW':
        return False
    return True


def select_mouse_button(context):
    keyconfig = context.window_manager.keyconfigs.active
    preferences = getattr(keyconfig, "preferences", None)
    return 'RIGHTMOUSE' if getattr(preferences, "select_mouse", 'LEFT') == 'RIGHT' else 'LEFTMOUSE'


def tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'DOPESHEET_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()

runtime = TimelineRuntimeState()
