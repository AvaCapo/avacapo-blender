# this to-be-big-ass-file is central for all state controls
# its done a simple "static-class" (class with no instance)
# called simple "State"
# as its not bpy thing, but a python-level thing it can be accesed
# from any thread.

# here is all async things, qeues etc...
from __future__ import annotations
import bpy
from dataclasses import dataclass, field
import uuid
from typing import Literal


class State:
    server_busy: bool = False
    server_status: str = ""
    current_fps: int = 24
    current_task_id: str = ""  # id of the task currently being fetched


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
        time_created: str = ""  # fill with datetime.now().isoformat() on creation

    tasks: list[Task] = []  # module-survives-reload caveat applies

    @staticmethod
    def generate_task_name(prompt: str) -> str:
        return prompt[:32].strip() or "Untitled"

    @classmethod
    def draw_task(cls, layout: bpy.types.UILayout | None, task: Task) -> None:
        box = layout.box()
        row = box.row()
        row.label(text=f"[{task.id}]")
        row.label(text=task.name)
        icon, label = STATUS_META.get(task.status, ("QUESTION", task.status))
        row.label(text=label, icon=icon)

    @classmethod
    def draw(cls, layout: bpy.types.UILayout | None) -> None:
        if not cls.tasks:
            layout.label(text="No tasks.", icon="INFO")
            return
        for task in cls.tasks:
            cls.draw_task(layout, task)

    @classmethod
    def add(cls, prompt: str) -> Task:
        task = cls.Task(
            name=cls.generate_task_name(prompt),
            prompt=prompt,
        )
        cls.tasks.append(task)
        return task

    @classmethod
    def discard(cls, task: Task) -> None:
        try:
            cls.tasks.remove(task)
        except ValueError:
            pass  # already removed, not an error

    @classmethod
    def get_by_id(cls, task_id: str) -> None:
        cls.tasks = [t for t in cls.tasks if t.id != task_id]


# Update handlers, which smartly update local state every click:
def update_handler(context): ...
