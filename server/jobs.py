"""Cancellable public API jobs. No Blender access or settings-file side effects."""

from email.utils import parsedate_to_datetime
import random
import time

import requests
from urllib3.exceptions import HTTPError

from ..common.config import Config
from ..core.generation_models import GenerationCancelled, GenerationError, TaskStatus


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise GenerationCancelled()


def _json_string(response, key: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        raise GenerationError("Server returned invalid JSON.") from None
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise GenerationError(f"Server response is missing '{key}'.")
    if len(value) > 4096:
        raise GenerationError("Server returned an oversized identifier or status.")
    return value


class AnimationJobs:
    """Submit, poll and download jobs using the configured AvaCapo API."""

    def __init__(self, *, base_url=None):
        self.base_url = (base_url or Config.SERVER_URL).rstrip("/")
        public_url = f"{self.base_url}/api/v1/public"
        self.status_url = f"{public_url}/api-avacapo-status/"
        self.download_url = f"{public_url}/api-avacapo-download/"

    def _remaining(self, deadline, stage):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GenerationError(f"Timed out during {stage}.")
        return remaining

    def _pause(self, delay, deadline, stage, cancel_event):
        _check_cancel(cancel_event)
        delay = min(delay, self._remaining(deadline, stage))
        if cancel_event is None:
            time.sleep(delay)
        elif cancel_event.wait(delay):
            raise GenerationCancelled()
        self._remaining(deadline, stage)

    def _timeout(self, deadline, read_timeout, stage):
        remaining = self._remaining(deadline, stage)
        return min(Config.CONNECT_TIMEOUT, remaining), min(read_timeout, remaining)

    @staticmethod
    def _http_error(response, stage):
        status = response.status_code
        return GenerationError(f"Animation API returned HTTP {status} during {stage}.")

    def _retry_delay(self, attempt, retry_after=None):
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except (ValueError, TypeError):
                try:
                    return max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        return Config.RETRY_BACKOFF * (2**attempt) * random.uniform(0.8, 1.2)

    def _get(
        self, url, *, params, expected, deadline, read_timeout, stage, cancel_event, stream=False
    ):
        for attempt in range(Config.GET_RETRY_ATTEMPTS):
            _check_cancel(cancel_event)
            retry_after = None
            try:
                response = requests.get(
                    url, params=params, stream=stream,
                    timeout=self._timeout(deadline, read_timeout, stage),
                )
            except requests.RequestException:
                error = GenerationError("Animation API network request failed.")
            else:
                if response.status_code in expected:
                    return response
                error = self._http_error(response, stage)
                retry_after = response.headers.get("Retry-After")
                response.close()
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    raise error
            if attempt + 1 == Config.GET_RETRY_ATTEMPTS:
                raise error from None
            self._pause(self._retry_delay(attempt, retry_after), deadline, stage, cancel_event)
        raise ValueError("GET_RETRY_ATTEMPTS must be positive.")

    def submit_job(self, url, *, cancel_event=None, **request_kwargs) -> str:
        """Submit exactly once. A lost POST response has an ambiguous remote outcome."""
        _check_cancel(cancel_event)
        try:
            with requests.post(
                url, timeout=(Config.CONNECT_TIMEOUT, Config.REQUEST_TIMEOUT),
                **request_kwargs,
            ) as response:
                if response.status_code != 202:
                    raise self._http_error(response, "submitting")
                try:
                    return _json_string(response, "hashed_id")
                except GenerationError:
                    raise GenerationError(
                        "Server accepted the submission but returned no usable job ID.",
                    ) from None
        except requests.RequestException:
            raise GenerationError(
                "Submission response was lost; the server may already have created the job.",
            ) from None

    def get_job_status(self, api_token, hashed_id, *, cancel_event=None, deadline=None) -> str:
        self._validate_identity(api_token, hashed_id)
        if deadline is None:
            deadline = time.monotonic() + Config.STATUS_REQUEST_TIMEOUT
        with self._get(
            self.status_url, params={"api_token": api_token, "hashed_id": hashed_id},
            expected={200}, deadline=deadline, read_timeout=Config.STATUS_REQUEST_TIMEOUT,
            stage="status", cancel_event=cancel_event,
        ) as response:
            status = _json_string(response, "status")
        self._remaining(deadline, "status")
        _check_cancel(cancel_event)
        if status not in {"pending", "processing", "ready", "failed"}:
            raise GenerationError("Server returned an unknown job status.")
        return status

    def wait_until_ready(self, api_token, hashed_id, *, cancel_event=None, on_progress=None):
        deadline = time.monotonic() + Config.GENERATION_WAIT_TIMEOUT
        previous_status = None
        while True:
            status = self.get_job_status(
                api_token, hashed_id, cancel_event=cancel_event, deadline=deadline
            )
            if status != previous_status and status in {"pending", "processing"} and on_progress:
                phase = (
                    TaskStatus.SERVER_PENDING if status == "pending" else TaskStatus.SERVER_PROCESSING
                )
                on_progress(phase, hashed_id)
            previous_status = status
            if status == "ready":
                return
            if status == "failed":
                raise GenerationError("Animation generation failed on the server.")
            self._pause(Config.POLL_INTERVAL, deadline, "status", cancel_event)

    def download_job(self, api_token, hashed_id, in_place=False, *, cancel_event=None) -> bytes:
        self._validate_identity(api_token, hashed_id)
        deadline = time.monotonic() + Config.DOWNLOAD_WAIT_TIMEOUT
        params = {
            "api_token": api_token, "hashed_id": hashed_id, "extension": "bvh",
            "include_skin": "false", "in_place": str(bool(in_place)).lower(),
        }
        read_failures = 0
        while True:
            try:
                with self._get(
                    self.download_url, params=params, expected={200, 202}, deadline=deadline,
                    read_timeout=Config.DOWNLOAD_REQUEST_TIMEOUT, stage="downloading",
                    cancel_event=cancel_event, stream=True,
                ) as response:
                    if response.status_code == 200:
                        content = bytearray()
                        while True:
                            _check_cancel(cancel_event)
                            self._remaining(deadline, "downloading")
                            # Read available data without waiting for a full 64 KiB block.
                            chunk = response.raw.read1(64 * 1024, decode_content=True)
                            if not chunk:
                                break
                            if len(content) + len(chunk) > Config.MAX_DOWNLOAD_BYTES:
                                raise GenerationError("Animation exceeds the download size limit.")
                            content.extend(chunk)
                        _check_cancel(cancel_event)
                        self._remaining(deadline, "downloading")
                        if not content:
                            raise GenerationError("Server returned an empty animation.")
                        return bytes(content)
            except (requests.RequestException, HTTPError):
                _check_cancel(cancel_event)
                self._remaining(deadline, "downloading")
                read_failures += 1
                if read_failures >= Config.GET_RETRY_ATTEMPTS:
                    raise GenerationError("Animation download was interrupted.") from None
                self._pause(
                    self._retry_delay(read_failures - 1), deadline, "downloading", cancel_event
                )
                continue
            self._pause(Config.POLL_INTERVAL, deadline, "downloading", cancel_event)

    def generate_animation(
        self, url, *, api_token, in_place=False, cancel_event=None, on_progress=None, **request_kwargs
    ) -> bytes:
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("An API token is required.")
        if on_progress:
            on_progress(TaskStatus.SUBMITTING, "")
        hashed_id = self.submit_job(url, cancel_event=cancel_event, **request_kwargs)
        # Publish the ID even if cancellation arrived while POST was in progress.
        if on_progress:
            on_progress(TaskStatus.SERVER_PENDING, hashed_id)
        _check_cancel(cancel_event)
        self.wait_until_ready(
            api_token, hashed_id, cancel_event=cancel_event, on_progress=on_progress
        )
        if on_progress:
            on_progress(TaskStatus.DOWNLOADING, hashed_id)
        return self.download_job(api_token, hashed_id, in_place, cancel_event=cancel_event)

    @staticmethod
    def _validate_identity(api_token, hashed_id):
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("An API token is required.")
        if not isinstance(hashed_id, str) or not hashed_id.strip():
            raise ValueError("A server job ID is required.")
