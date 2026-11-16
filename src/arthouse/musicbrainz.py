"""MusicBrainz music enrichment: client skeleton and transport policy.

MusicBrainz asks for a contact-bearing User-Agent and at most one request per
second. This module owns that policy; the queries and the enrichment pass land
on top of it in later steps.
"""

import time
from collections.abc import Callable
from types import TracebackType
from typing import Self, cast

import httpx

from arthouse.config import get_settings

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2/"
MAX_ATTEMPTS = 4
RETRY_BACKOFF = 1.0  # seconds; doubled per attempt when no Retry-After is sent
REQUEST_INTERVAL = 1.0  # MusicBrainz's documented rate limit


class MusicBrainzClient:
    """Thin MusicBrainz WS/2 client with a 1 req/s throttle and retries."""

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        timeout: float = 20.0,
        interval: float = REQUEST_INTERVAL,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if user_agent is None:
            user_agent = get_settings().musicbrainz_user_agent
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._client: httpx.Client = httpx.Client(
            base_url=MUSICBRAINZ_BASE_URL,
            headers=headers,
            params={"fmt": "json"},
            timeout=timeout,
            transport=transport,
        )
        self._interval: float = interval
        self._clock: Callable[[], float] = clock
        self._sleep: Callable[[float], None] = sleep
        self._last_request: float | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def _throttle(self) -> None:
        """Wait out the remainder of the interval before the next request."""
        now = self._clock()
        if self._last_request is not None:
            delay = self._interval - (now - self._last_request)
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
        self._last_request = now

    def _get(self, path: str, params: dict[str, str | int] | None = None) -> object:
        """GET with throttling and retries, so one 429/5xx does not abort a batch."""
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            response = self._client.get(path.lstrip("/"), params=params)
            transient = response.status_code == 429 or response.status_code >= 500
            if transient and attempt < MAX_ATTEMPTS - 1:
                # httpx types Headers.get as Any (it forwards an untyped default).
                retry_after = cast(str | None, response.headers.get("Retry-After"))
                wait: float
                try:
                    wait = (
                        float(retry_after)
                        if retry_after is not None
                        else RETRY_BACKOFF * 2.0**attempt
                    )
                except ValueError:
                    wait = RETRY_BACKOFF * 2.0**attempt
                self._sleep(wait)
                continue
            response.raise_for_status()
            return cast(object, response.json())
        raise AssertionError("unreachable: the last attempt either returns or raises")
