from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import bpy


class State:
    server_busy: bool = False
    server_status: str = ""
    current_fps: int = 24
    current_task_id: str = ""


TaskStatus = Literal["pending", "loading", "done", "aborted", "error"]

STATUS_META: dict[str, tuple[str, str]] = {
    "pending": ("TIME", "Pending"),
    "loading": ("SORTTIME", "Loading..."),
    "done": ("CHECKMARK", "Done"),
    "aborted": ("CANCEL", "Aborted"),
    "error": ("ERROR", "Error"),
}


class Queue:
    TERMINAL_STATUSES = frozenset({"done", "error", "aborted"})
    MAX_TASKS: int | None = None

    @dataclass
    class Task:
        name: str
        prompt: str
        status: TaskStatus = "pending"
        id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
        time_created: str = field(
            default_factory=lambda: datetime.now().isoformat(timespec="seconds")
        )
        time_finished: str = ""
        generation_time: float = 0.0
        start_frame: int = 0
        duration: float = 0.0
        model: str = ""

    tasks: list[Task] = []
    _tasks_by_id: dict[str, Task] = {}
    allow_new_task: bool = True

    @staticmethod
    def generate_task_name(prompt: str) -> str:
        return prompt[:32].strip() or "Untitled"

    @classmethod
    def _refresh_allow_new_task(cls) -> None:
        cls.allow_new_task = cls.MAX_TASKS is None or len(cls.tasks) < cls.MAX_TASKS

    @classmethod
    def add(
        cls,
        prompt: str,
        start_frame: int = 0,
        duration: float = 0.0,
        model: str = "",
    ) -> "Queue.Task":
        cls._refresh_allow_new_task()
        if not cls.allow_new_task:
            raise ValueError("Queue is full.")

        task = cls.Task(
            name=cls.generate_task_name(prompt),
            prompt=prompt,
            start_frame=start_frame,
            duration=duration,
            model=model,
        )
        cls.tasks.append(task)
        cls._tasks_by_id[task.id] = task
        cls._refresh_allow_new_task()
        return task

    @classmethod
    def clone_task(cls, task: "Queue.Task") -> "Queue.Task":
        return cls.add(
            prompt=task.prompt,
            start_frame=task.start_frame,
            duration=task.duration,
            model=task.model,
        )

    @classmethod
    def clone(cls, task_id: str) -> "Queue.Task | None":
        task = cls.get_by_id(task_id)
        if task is None:
            return None
        return cls.clone_task(task)

    @classmethod
    def get_by_id(cls, task_id: str) -> "Queue.Task | None":
        return cls._tasks_by_id.get(task_id)

    @classmethod
    def next_pending(cls) -> "Queue.Task | None":
        return next((task for task in cls.tasks if task.status == "pending"), None)

    @classmethod
    def start_next(cls) -> "Queue.Task | None":
        task = cls.next_pending()
        if task is None:
            return None
        task.status = "loading"
        return task

    @classmethod
    def set_status(cls, task_id: str, status: TaskStatus) -> "Queue.Task | None":
        task = cls.get_by_id(task_id)
        if task is None:
            return None
        task.status = status
        return task

    @classmethod
    def finish(
        cls,
        task_id: str,
        *,
        status: TaskStatus,
        generation_time: float | None = None,
    ) -> "Queue.Task | None":
        task = cls.set_status(task_id, status)
        if task is None:
            return None

        task.time_finished = datetime.now().isoformat(timespec="seconds")
        if generation_time is not None:
            task.generation_time = generation_time
        return task

    @classmethod
    def discard(cls, task: "Queue.Task") -> None:
        cls.discard_by_id(task.id)

    @classmethod
    def discard_by_id(cls, task_id: str) -> None:
        task = cls._tasks_by_id.pop(task_id, None)
        if task is None:
            return

        try:
            cls.tasks.remove(task)
        except ValueError:
            cls.tasks = [existing for existing in cls.tasks if existing.id != task_id]

        cls._refresh_allow_new_task()


# application handlers:
# ---------------------
# https://docs.blender.org/api/current/bpy.app.handlers.html


def frame_change_post(scene):
    if bpy.context.screen.is_animation_playing:
        return
    if scene.avacapo_settings.start_record_lock:
        scene.avacapo_settings.start = scene.frame_current
