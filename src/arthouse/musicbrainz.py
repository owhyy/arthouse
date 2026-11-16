"""MusicBrainz music enrichment: client, transport policy, and typed queries.

MusicBrainz asks for a contact-bearing User-Agent and at most one request per
second. This module owns that policy and the read queries the enrichment pass
builds on; matching and the batch itself land in later steps.
"""

import time
from collections.abc import Callable
from types import TracebackType
from typing import NotRequired, Self, TypedDict, cast

import httpx

from arthouse.config import get_settings

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2/"
MAX_ATTEMPTS = 4
RETRY_BACKOFF = 1.0  # seconds; doubled per attempt when no Retry-After is sent
REQUEST_INTERVAL = 1.0  # MusicBrainz's documented rate limit
SEARCH_LIMIT = 5  # candidates per search; enough to disambiguate by year


# These types describe the slice of the payload the enrichment pass relies on,
# not the whole wire format: a field that nothing reads stays out, so reading it
# later is a type error rather than a silent assumption. `count` is in because
# the mapping layer keeps only tags the community has voted up (count > 0); the
# detail endpoint omits the rest already, the search endpoint does not.


class Tag(TypedDict):
    """A community tag; `count` is the net vote tally, kept so it can be filtered."""

    name: str
    count: int


class Genre(TypedDict):
    """A curated genre; the same shape as a tag."""

    name: str
    count: int


class ArtistRef(TypedDict):
    """Just enough of an artist to fetch its genres and tags."""

    id: str


class ArtistCredit(TypedDict):
    """One credited artist; `name` is the credited form, not the canonical one."""

    name: str
    artist: NotRequired[ArtistRef]


class Artist(TypedDict, total=False):
    """An artist's own genres and tags, used only as a fallback."""

    name: str
    tags: list[Tag]
    genres: list[Genre]


# `first-release-date` and `artist-credit` are hyphenated keys, so this entity
# needs the functional TypedDict syntax. `artist-credit` and `genres` are absent
# from one payload or the other rather than merely empty, so they stay optional.
ReleaseGroup = TypedDict(
    "ReleaseGroup",
    {
        "id": str,
        "title": str,
        "first-release-date": NotRequired[str],
        "artist-credit": NotRequired[list[ArtistCredit]],
        "genres": NotRequired[list[Genre]],
        "tags": NotRequired[list[Tag]],
    },
)


# The search wrapper; an empty result omits `release-groups` entirely.
ReleaseGroupSearch = TypedDict(
    "ReleaseGroupSearch",
    {"release-groups": list[ReleaseGroup]},
    total=False,
)


def _quoted(term: str) -> str:
    """Escape a Lucene term, so a quote in a title cannot break the query."""
    return term.replace("\\", "\\\\").replace('"', '\\"')


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

    def search_release_groups(
        self, title: str, artist: str = "", *, limit: int = SEARCH_LIMIT
    ) -> list[ReleaseGroup]:
        """Release-group candidates for a title, narrowed by artist when known.

        The search engine only orders by score, and score is not a match: the
        caller re-checks the title and uses the year only as a tie-break,
        because MusicBrainz, like TMDB, treats a date filter loosely. Hits carry
        community tags (including net-rejected ones) but never curated genres.
        """
        clauses = [f'releasegroup:"{_quoted(title)}"']
        if artist:
            clauses.append(f'artist:"{_quoted(artist)}"')
        params: dict[str, str | int] = {"query": " AND ".join(clauses), "limit": limit}
        payload = cast(ReleaseGroupSearch, self._get("/release-group", params))
        return payload.get("release-groups", [])

    def release_group(self, mbid: str) -> ReleaseGroup:
        """One release group with its curated genres and community tags.

        `inc=genres` is an entity-lookup feature, which is why the search hit
        cannot stand in for this call. `artist-credit` would need its own `inc`
        term; the artist id for the fallback is taken from the search hit.
        """
        payload = cast(ReleaseGroup, self._get(f"/release-group/{mbid}", {"inc": "genres+tags"}))
        return payload

    def artist(self, mbid: str) -> Artist:
        """One artist with genres and tags, used when a release group has none."""
        payload = cast(Artist, self._get(f"/artist/{mbid}", {"inc": "genres+tags"}))
        return payload
