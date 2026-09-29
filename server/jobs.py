"""Submit public API jobs and download their completed animation as bytes."""

import time
import requests

from ..common.config import Config
from ..common.logger import log

config = Config()


def _check_response(response, expected_status: int, operation: str) -> None:
    if response.status_code != expected_status:
        # URLs and response bodies can contain the API token.
        raise RuntimeError(f"{operation} failed: HTTP {response.status_code}.")


def _json_string(response, key: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Server returned invalid JSON.") from None
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Server response is missing a valid '{key}'.")
    return value


def _remaining(deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Timed out waiting for {operation}.")
    return remaining


def _pause(deadline: float, operation: str) -> None:
    time.sleep(min(config.POLL_INTERVAL, _remaining(deadline, operation)))


def _wait_until_ready(api_token: str, hashed_id: str) -> None:
    deadline = time.monotonic() + config.GENERATION_WAIT_TIMEOUT
    previous_status = None
    while True:
        remaining = _remaining(deadline, "animation generation")
        with requests.get(
            config.STATUS_URL,
            params={"api_token": api_token, "hashed_id": hashed_id},
            timeout=(
                min(config.CONNECT_TIMEOUT, remaining),
                min(config.STATUS_REQUEST_TIMEOUT, remaining),
            ),
        ) as response:
            _check_response(response, 200, "Animation status request")
            status = _json_string(response, "status")
        
        if status not in {"pending", "processing", "ready", "failed"}:
            raise RuntimeError(f"Server returned an unknown animation status.")
        if status != previous_status:
            log.info("Animation generation status: %s", status)
            previous_status = status
        if status == "ready":
            return
        if status == "failed":
            raise RuntimeError("Animation generation failed on the server.")
        _pause(deadline, "animation generation")


def _download_animation(api_token: str, hashed_id: str, in_place: bool) -> bytes:
    deadline = time.monotonic() + config.DOWNLOAD_WAIT_TIMEOUT
    params = {
        "api_token": api_token,
        "hashed_id": hashed_id,
        "extension": "bvh",
        "include_skin": "false",
        "in_place": str(bool(in_place)).lower(),
    }
    while True:
        remaining = _remaining(deadline, "animation download")
        with requests.get(
            config.DOWNLOAD_URL,
            params=params,
            stream=True,
            timeout=(
                min(config.CONNECT_TIMEOUT, remaining),
                min(config.DOWNLOAD_REQUEST_TIMEOUT, remaining),
            ),
        ) as response:
            if response.status_code != 202:
                _check_response(response, 200, "Animation download")
                _remaining(deadline, "animation download")
                content = response.content
                _remaining(deadline, "animation download")
                if not content:
                    raise RuntimeError("Server returned an empty animation file.")
                return content
        _pause(deadline, "animation download")


def generate_animation(url: str, *, api_token: str, in_place: bool, **request_kwargs) -> bytes:
    """Create a job, wait for it and return BVH bytes using the same auth token."""
    try:
        with requests.post(
            url,
            timeout=(config.CONNECT_TIMEOUT, config.REQUEST_TIMEOUT),
            **request_kwargs,
        ) as response:
            _check_response(response, 202, "Animation submission")
            hashed_id = _json_string(response, "hashed_id")
        _wait_until_ready(api_token, hashed_id)
        return _download_animation(api_token, hashed_id, in_place)
    except requests.RequestException:
        raise RuntimeError("Animation API request failed due to a network error.") from None
