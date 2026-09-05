"""Shared HTTP helper — the ONLY place in the project that makes network calls.

All sources must use get_json(). No bare requests.get() anywhere else.
"""

from __future__ import annotations

import time

import requests

_USER_AGENT = "EdgeDash/0.1 (career intelligence agent; github.com/edgedash)"
_DEFAULT_TIMEOUT = 10       # seconds
_MAX_RETRIES = 2
_BACKOFF_BASE = 1.5         # seconds; wait = base ** attempt  (1.5s, 2.25s)


class SourceError(Exception):
    """Raised when a source HTTP call fails after all retries."""


def get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict | list:
    """GET url, parse JSON, retry on transient errors.

    Raises SourceError if all attempts fail.
    """
    merged_headers = {"User-Agent": _USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            wait = _BACKOFF_BASE ** attempt
            time.sleep(wait)

        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout as exc:
            last_exc = exc
        except requests.exceptions.HTTPError as exc:
            # 4xx errors won't be fixed by a retry.
            raise SourceError(
                f"HTTP {exc.response.status_code} from {url}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            last_exc = exc

    raise SourceError(
        f"Failed to GET {url} after {_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc
