"""A small, explicitly started generation queue with a bounded worker pool."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Condition, Event, RLock
import time
import uuid

from ..common.config import Config
from ..server import client
from .generation_models import (
    GenerationCancelled, GenerationCommand, GenerationError, GenerationMode,
    GenerationResult,
    TaskSnapshot, TaskStatus, TERMINAL_STATUSES,
)


class GenerationQueue:
    def __init__(self, *, max_concurrent=1, max_tasks=100):
        self.max_concurrent = max_concurrent
        self.max_tasks = max_tasks
        self.runtime_id = uuid.uuid4().hex
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._executor = None
        self._accepting = True
        self._stopping = False
        self._tasks = {}
        self._pending = {}
        self._futures = {}
        self._cancel_events = {}
        self._results = {}
        self._events = {}

    def _set_task(self, task_id, **changes):
        task = replace(self._tasks[task_id], **changes)
        self._tasks[task_id] = self._events[task_id] = task
        self._changed.notify_all()

    def submit(self, command: GenerationCommand, *, api_token: str) -> str:
        if not isinstance(command, GenerationCommand):
            raise ValueError("A GenerationCommand is required.")
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("An API token is required.")
        command = command.resolve_seed()
        with self._lock:
            if not self._accepting:
                raise RuntimeError("The generation queue is stopped.")
            if len(self._tasks) >= self.max_tasks:
                raise RuntimeError("The queue is full; discard completed tasks to free space.")
            task_id = uuid.uuid4().hex
            task = TaskSnapshot(
                task_id, self.runtime_id, command.mode, command.name, command.prompt,
                TaskStatus.QUEUED, "", time.time(), None, None,
                getattr(command.parameters, "seed", None), None,
            )
            self._tasks[task_id] = self._events[task_id] = task
            self._cancel_events[task_id] = Event()
            self._pending[task_id] = (command, api_token)
            self.update_cycle()
            return task_id

    def start(self):
        with self._lock:
            if self._stopping:
                raise RuntimeError("The queue is still stopping.")
            if self._executor is not None:
                if not self._accepting:
                    raise RuntimeError("Call stop(wait=True) before restarting.")
                return
            self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent)
            self._accepting = True
            self.update_cycle()

    def update_cycle(self):
        """Start pending commands in FIFO order as worker slots become available."""
        with self._lock:
            if self._executor is None or not self._accepting:
                return
            for task_id, (command, token) in tuple(self._pending.items()):
                if len(self._futures) >= self.max_concurrent:
                    break
                self._pending.pop(task_id)
                try:
                    future = self._executor.submit(
                        self._execute, task_id, command, token, self._cancel_events[task_id]
                    )
                except Exception:
                    self._cancel_events.pop(task_id)
                    self._set_task(task_id, status=TaskStatus.FAILED, finished_at=time.time(),
                                   error="Could not start the generation worker.")
                    continue
                self._futures[task_id] = future
                future.add_done_callback(lambda _, uid=task_id: self._worker_done(uid))

    def _progress(self, task_id, status, job_id):
        with self._lock:
            if task_id not in self._futures:
                return
            task = self._tasks[task_id]
            phase = TaskStatus(status)
            if phase in TERMINAL_STATUSES or phase == TaskStatus.QUEUED:
                raise ValueError("Workers may report execution phases only.")
            if job_id and task.server_job_id and job_id != task.server_job_id:
                raise ValueError("A worker cannot change the server job ID.")
            # Retain a late POST ID on cancellation without reviving the cancelled task.
            self._set_task(
                task_id, status=task.status if task.status in TERMINAL_STATUSES else phase,
                server_job_id=job_id or task.server_job_id,
            )

    def _execute(self, task_id, command, token, cancel_event):
        content = None
        error = None
        try:
            with self._lock:
                if cancel_event.is_set():
                    raise GenerationCancelled()
                self._set_task(task_id, status=TaskStatus.SUBMITTING, started_at=time.time())
            content = self._generate(
                command, api_token=token, cancel_event=cancel_event,
                on_progress=lambda status, job_id: self._progress(task_id, status, job_id),
            )
            if not isinstance(content, bytes) or not content:
                raise GenerationError("The server returned no animation.")
            if len(content) > Config.MAX_DOWNLOAD_BYTES:
                raise GenerationError("Animation exceeds the size limit.")
            status = TaskStatus.SUCCEEDED
        except GenerationCancelled:
            status = TaskStatus.CANCELLED
        except GenerationError as exc:
            status, error = TaskStatus.FAILED, str(exc)
        except Exception:
            # Arbitrary exception messages may contain credentials.
            status = TaskStatus.FAILED
            error = "Animation generation failed."
        with self._lock:
            task = self._tasks[task_id]
            if task.status in TERMINAL_STATUSES:
                return
            if cancel_event.is_set():
                status, error = TaskStatus.CANCELLED, None
            if status == TaskStatus.SUCCEEDED:
                self._results[task_id] = GenerationResult(
                    task_id, task.server_job_id, content, task.resolved_seed
                )
            self._set_task(task_id, status=status, error=error, finished_at=time.time())

    def _generate(self, command, *, api_token, cancel_event, on_progress):
        """Call the AvaCapo endpoint for this command's mode."""
        parameters = asdict(command.parameters)
        if command.mode == GenerationMode.CONSTRAINTS:
            constraints = parameters.pop("constraints")
            parameters["motion_files"] = [item.pop("motion_file") for item in constraints]
            parameters["pose_constraints"] = constraints
        elif command.mode == GenerationMode.INBETWEEN:
            parameters["left_context_pose"] = parameters.pop("left_motion")
            parameters["right_context_pose"] = parameters.pop("right_motion")
            parameters["inbetween_frames"] = parameters.pop("gap_frames")
        generate = {
            GenerationMode.TEXT: client.get_animation,
            GenerationMode.CONSTRAINTS: client.get_animation_constraints,
            GenerationMode.INBETWEEN: client.get_animation_inbetween,
        }[command.mode]
        return generate(
            name=command.name, prompt=command.prompt, model=command.model,
            in_place=command.in_place, api_token=api_token,
            cancel_event=cancel_event, on_progress=on_progress, **parameters,
        )

    def _worker_done(self, task_id):
        with self._lock:
            self._futures.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self.update_cycle()
            self._changed.notify_all()

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            if self._tasks[task_id].status in TERMINAL_STATUSES:
                return False
            self._cancel_events[task_id].set()
            self._set_task(task_id, status=TaskStatus.CANCELLED, finished_at=time.time())
            self._pending.pop(task_id, None)
            future = self._futures.get(task_id)
            if future is not None:
                future.cancel()  # Running work keeps its pool slot until it actually exits.
            else:
                self._cancel_events.pop(task_id)
            return True

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._lock:
            return self._tasks[task_id]

    def list_tasks(self) -> tuple[TaskSnapshot, ...]:
        with self._lock:
            return tuple(self._tasks.values())

    def get_result(self, task_id: str) -> GenerationResult | None:
        with self._lock:
            self._tasks[task_id]  # Distinguish a missing task from a result that is not ready.
            return self._results.get(task_id)

    def release_result(self, task_id: str) -> None:
        with self._lock:
            self._tasks[task_id]
            self._results.pop(task_id, None)

    def discard(self, task_id: str) -> None:
        with self._lock:
            if self._tasks[task_id].status not in TERMINAL_STATUSES or task_id in self._futures:
                raise RuntimeError("Wait for the task's worker to exit before discarding it.")
            self._tasks.pop(task_id)
            self._results.pop(task_id, None)
            self._events.pop(task_id, None)

    def drain_events(self) -> tuple[TaskSnapshot, ...]:
        with self._lock:
            events = tuple(self._events.values())
            self._events.clear()
            return events

    def wait(self, task_id: str, timeout: float | None = None) -> TaskSnapshot:
        """Blocking helper for scripts; do not call from Blender's interactive main loop."""
        with self._changed:
            if not self._changed.wait_for(
                lambda: self._tasks[task_id].status in TERMINAL_STATUSES
                and task_id not in self._futures, timeout=timeout
            ):
                raise TimeoutError("Timed out waiting for the local generation task.")
            return self._tasks[task_id]

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._stopping:
                raise RuntimeError("A queue stop is already in progress.")
            self._stopping = True
            self._accepting = False
            executor = self._executor
            for uid in tuple(self._tasks):
                self.cancel(uid)
        joined = False
        try:
            if executor is not None:
                executor.shutdown(wait=wait, cancel_futures=True)
            joined = wait
        finally:
            with self._lock:
                self._executor = None if joined else executor
                self._stopping = False

    def reset(self) -> None:
        self.stop()
        with self._lock:
            self._tasks.clear()
            self._results.clear()
            self._events.clear()
            self.runtime_id = uuid.uuid4().hex

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(future.running() for future in self._futures.values())

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending) + sum(
                not future.running() and not future.done() for future in self._futures.values()
            )
