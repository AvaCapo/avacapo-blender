"""Blender context helpers and temporary timeline UI state."""

from dataclasses import dataclass, field
from time import monotonic

import bpy

from ..core.schemas import ClipData


@dataclass(frozen=True)
class HoverTarget:
    kind: str = "NONE"
    track_uid: int = 0
    uid: int = 0
    other_uid: int = 0


@dataclass(frozen=True)
class HoverHint:
    region_pointer: int = 0
    mouse_x: int = 0
    mouse_y: int = 0
    visible: bool = False


@dataclass(frozen=True)
class MenuTarget:
    frame: float = 0.0
    track_uid: int = 0
    clip_uid: int = 0


@dataclass
class ClipboardTake:
    action: bpy.types.Action
    slot_handle: int
    source_text: bpy.types.Text | None


@dataclass
class ClipboardClip:
    data: ClipData
    takes: dict[int, ClipboardTake]

    def release(self):
        for copied_take in self.takes.values():
            action = copied_take.action
            if action.users == 0:
                bpy.data.actions.remove(action)
            text = copied_take.source_text
            if text is not None and text.users == 0:
                bpy.data.texts.remove(text)
        self.takes.clear()


@dataclass
class TimelineRuntimeState:
    hover: HoverTarget = field(default_factory=HoverTarget)
    hover_hint: HoverHint = field(default_factory=HoverHint)
    hover_timer: bpy.types.Timer | None = None
    menu_target: MenuTarget = field(default_factory=MenuTarget)
    clipboard: ClipboardClip | None = None
    generation_cooldown_until: float = 0.0

    def generation_ready(self):
        return monotonic() >= self.generation_cooldown_until

    def set_clipboard(self, clipboard: ClipboardClip):
        if isinstance(self.clipboard, ClipboardClip):
            self.clipboard.release()
        self.clipboard = clipboard

    def clear_hover(self):
        if self.hover_timer is not None:
            try:
                bpy.context.window_manager.event_timer_remove(self.hover_timer)
            except (ReferenceError, RuntimeError):
                pass
            self.hover_timer = None
        self.hover = HoverTarget()
        self.hover_hint = HoverHint()

    def reset(self):
        self.generation_cooldown_until = 0.0
        self.clear_hover()
        self.menu_target = MenuTarget()
        if isinstance(self.clipboard, ClipboardClip):
            self.clipboard.release()
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


def has_selected_armature(context):
    armature = context.active_object
    return armature is not None and armature.type == 'ARMATURE' and armature.select_get()


def tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'DOPESHEET_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()

runtime = TimelineRuntimeState()
