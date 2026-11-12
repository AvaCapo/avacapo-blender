"""Programmatic generation facade. Constructing/importing it starts no workers."""

from .task_queue import GenerationQueue


class GenerationService(GenerationQueue):
    def __init__(self, *, api_token: str, max_concurrent=5, max_tasks=100):
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("An API token is required.")
        self._api_token = api_token
        super().__init__(max_concurrent=max_concurrent, max_tasks=max_tasks)

    def submit(self, command) -> str:
        """Capture one token per task; later credential changes affect only new submissions."""
        with self._lock:
            return super().submit(command, api_token=self._api_token)

    def set_api_token(self, api_token: str) -> None:
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("An API token is required.")
        with self._lock:
            self._api_token = api_token

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
