# this to-be-big-ass-file is central for all state controls
# its done a simple "static-class" (class with no instance)
# called simple "State"
# as its not bpy thing, but a python-level thing it can be accesed
# from any thread.

# here is all async things, qeues etc...
from __future__ import annotations
import bpy
from dataclasses import dataclass, field
from datetime import datetime
import uuid
from typing import Literal


class State:
    server_busy: bool = False
    server_status: str = ""
    current_fps: int = 24
    current_task_id: str = ""


TaskStatus = Literal["pending", "loading", "done", "aborted", "error"]

STATUS_META: dict[str, tuple[str, str]] = {
    "pending": ("TIME", "Pending"),
    "loading": ("SORTTIME", "Loading…"),
    "done": ("CHECKMARK", "Done"),
    "aborted": ("CANCEL", "Aborted"),
    "error": ("ERROR", "Error"),
}


class Queue:
    @dataclass
    class Task:
        name: str
        prompt: str
        status: TaskStatus = "pending"
        id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
        # timing
        time_created: str = field(
            default_factory=lambda: datetime.now().isoformat(timespec="seconds")
        )
        time_finished: str = ""
        generation_time: float = 0.0  # seconds, wall-clock
        # generation params snapshot (so redo is reproducible)
        start_frame: int = 0
        duration: float = 0.0
        model: str = ""

    tasks: list[Task] = []

    @staticmethod
    def generate_task_name(prompt: str) -> str:
        return prompt[:32].strip() or "Untitled"

    @classmethod
    def draw_task(cls, layout: bpy.types.UILayout, task: "Queue.Task") -> None:
        box = layout.box()

        # ── row 1: id · status · gen time ────────────────────────────
        row = box.row(align=True)
        icon, status_label = STATUS_META.get(task.status, ("QUESTION", task.status))
        row.label(text=f"[{task.id}]")
        row.label(text=status_label, icon=icon)
        if task.generation_time > 0:
            row.label(text=f"{task.generation_time:.1f}s", icon="TEMP")

        # ── row 2: prompt preview ─────────────────────────────────────
        row2 = box.row()
        row2.label(
            text=task.prompt[:48] + ("…" if len(task.prompt) > 48 else ""), icon="TEXT"
        )

        # ── row 3: params snapshot ────────────────────────────────────
        row3 = box.row(align=True)
        row3.label(text=f"frame {task.start_frame}", icon="KEYFRAME")
        row3.label(text=f"{task.duration}s", icon="TIME")
        row3.label(text=task.model, icon="SHADERFX")

        # ── row 4: redo / discard (only when not running) ────────────
        if task.status in ("done", "error", "aborted"):
            row4 = box.row(align=True)
            op = row4.operator("queue.redo_task", text="Redo", icon="FILE_REFRESH")
            op.task_id = task.id
            op2 = row4.operator("queue.discard_task", text="", icon="X")
            op2.task_id = task.id

    @classmethod
    def draw(cls, layout: bpy.types.UILayout) -> None:
        if not cls.tasks:
            layout.label(text="No tasks.", icon="INFO")
            return
        for task in cls.tasks:
            cls.draw_task(layout, task)

    @classmethod
    def add(
        cls, prompt: str, start_frame: int = 0, duration: float = 0.0, model: str = ""
    ) -> "Queue.Task":
        task = cls.Task(
            name=cls.generate_task_name(prompt),
            prompt=prompt,
            start_frame=start_frame,
            duration=duration,
            model=model,
        )
        cls.tasks.append(task)
        return task

    @classmethod
    def get_by_id(cls, task_id: str) -> "Queue.Task | None":
        return next((t for t in cls.tasks if t.id == task_id), None)

    @classmethod
    def discard(cls, task: "Queue.Task") -> None:
        try:
            cls.tasks.remove(task)
        except ValueError:
            pass

    @classmethod
    def discard_by_id(cls, task_id: str) -> None:
        cls.tasks = [t for t in cls.tasks if t.id != task_id]


def update_handler(context): ...
