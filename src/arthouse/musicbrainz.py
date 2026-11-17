"""MusicBrainz music enrichment: client, transport policy, queries and matching.

MusicBrainz asks for a contact-bearing User-Agent and at most one request per
second. This module owns that policy, the read queries, and the strict matcher
that turns search hits into one release group; the batch itself lands later.
"""

import html
import logging
import re
import time
from collections.abc import Callable
from types import TracebackType
from typing import NotRequired, Protocol, Self, TypedDict, cast

import httpx

from arthouse.config import get_settings
from arthouse.library import normalize_title
from arthouse.logs import response_headers

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2/"
MAX_ATTEMPTS = 4
REQUEST_INTERVAL = 1.0  # MusicBrainz's documented rate limit
THROTTLE_STEP = 1.0  # each transient failure slows the next request by this much
THROTTLE_MAX = 5.0  # ceiling, so a bad patch cannot stall a whole run
SEARCH_LIMIT = 5  # candidates per search; enough to disambiguate by year

log = logging.getLogger(__name__)


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


class ArtistCredit(TypedDict):
    """One credited artist; `name` is the credited form, not the canonical one."""

    name: str
    artist: NotRequired[dict[str, str]]


class Artist(TypedDict, total=False):
    """An artist's own genres, used only as a display fallback."""

    name: str
    genres: list[Genre]


# `first-release-date` and `artist-credit` are hyphenated keys, so this entity
# needs the functional TypedDict syntax. `artist-credit` and `genres` are absent
# from one payload or the other rather than merely empty, so they stay optional.
ReleaseGroup = TypedDict(
    "ReleaseGroup",
    {
        "id": str,
        "title": str,
        "primary-type": NotRequired[str],
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
    """Escape a term, so a quote in a title cannot break the query."""
    return term.replace("\\", "\\\\").replace('"', '\\"')


def release_year(group: ReleaseGroup) -> int | None:
    """The year of the group's first release, when MusicBrainz dates it."""
    date = group.get("first-release-date") or ""
    return int(date[:4]) if date[:4].isdigit() else None


def match_release_group(
    hits: list[ReleaseGroup], title: str, year: int | None
) -> ReleaseGroup | None:
    """Pick the one candidate whose title matches exactly.

    A hit is only accepted when its normalized title equals the item's. An album
    wins over a single or EP of the same name, because the library is albums and
    a same-year single would otherwise make the title ambiguous. Year is a
    tie-break rather than a filter, since reissues move the date by more than a
    year; a tie that survives both rules is reported as a miss, never guessed.
    """
    wanted = normalize_title(html.unescape(title))
    if not wanted:
        # A title made only of punctuation has no usable key: matching on ""
        # would accept any other symbol-only title, so report the miss instead.
        return None
    exact = [hit for hit in hits if normalize_title(html.unescape(hit["title"])) == wanted]
    if not exact:
        return None
    albums = [hit for hit in exact if hit.get("primary-type") == "Album"]
    pool = albums or exact
    if year is None:
        return pool[0] if len(pool) == 1 else None
    dated = [
        (abs(released - year), group)
        for group in pool
        if (released := release_year(group)) is not None
    ]
    if not dated:
        # An undated hit cannot be checked against the item's year, and guessing
        # would be worse than reporting the miss.
        return None
    best = min(distance for distance, _ in dated)
    winners = [group for distance, group in dated if distance == best]
    return winners[0] if len(winners) == 1 else None


# Credits join artists with these; a band name such as "Black Country, New Road"
# contains one too, which is why the full credit is always tried first.
_ARTIST_SPLIT = re.compile(r"\s+(?:featuring|&|and)\s+|,", re.IGNORECASE)


def artist_queries(creator: str) -> list[str]:
    """The full credit first, then its parts, for the search artist clause.

    Bodies like "Black Country, New Road" and "Tyler, The Creator" are single
    artist names, so the whole credit is always tried first; the split is only
    a fallback for real joins such as "Boldy James & Sterling Toles", whose
    exact string matches nothing in MusicBrainz.
    """
    full = html.unescape(creator).strip()
    parts = [part.strip() for part in _ARTIST_SPLIT.split(full) if part.strip()]
    return list(dict.fromkeys([full, *parts]))


def find_release_group(
    client: ReleaseGroupSearcher, title: str, creator: str, year: int | None
) -> ReleaseGroup | None:
    """Search each artist variant, then match exactly.

    The title is unescaped before it becomes a query term: RYM stores
    `&#34;Heroes&#34;`, it gets tokenized into `34 heroes 34`, which
    matches nothing. There is deliberately no artist-less retry: matching checks
    the title and year only, so dropping the artist clause could accept a
    different artist's album with the same name.
    """
    query_title = html.unescape(title)
    for artist in artist_queries(creator):
        hits = client.search_release_groups(query_title, artist)
        if match := match_release_group(hits, title, year):
            return match
    return None


class ReleaseGroupSearcher(Protocol):
    """Anything that can search release groups, whatever the upstream API."""

    def search_release_groups(
        self, title: str, artist: str = "", *, limit: int = SEARCH_LIMIT
    ) -> list[ReleaseGroup]: ...


class MusicClient(ReleaseGroupSearcher, Protocol):
    """Everything one album enrichment needs from a music provider."""

    def release_group(self, mbid: str) -> ReleaseGroup: ...

    def artist(self, mbid: str) -> Artist: ...


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
        self._base_interval: float = interval
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
                log.debug("throttle: sleeping %.2fs", delay)
                self._sleep(delay)
                now = self._clock()
        self._last_request = now

    def _slow_down(self) -> None:
        """Widen the interval after a transient failure, up to the cap."""
        self._interval = min(self._interval + THROTTLE_STEP, THROTTLE_MAX)
        log.debug("throttle: raised to %.1fs", self._interval)

    def _speed_up(self) -> None:
        """Ease the interval back towards the base after a response that worked."""
        self._interval = max(self._base_interval, self._interval - THROTTLE_STEP)

    def _get(self, path: str, params: dict[str, str | int] | None = None) -> object:
        """GET with throttling and retries, so one 429/5xx does not abort a batch."""
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            log.debug("GET %s %s", path, params or {})
            response = self._client.get(path.lstrip("/"), params=params)
            transient = response.status_code == 429 or response.status_code >= 500
            if transient and attempt < MAX_ATTEMPTS - 1:
                # The throttle is the whole retry policy: MusicBrainz answers a
                # busy 503 with `Retry-After: 0`, and its rate-limit headers
                # describe a global per-second bucket, so neither says how long
                # to wait. They are logged for diagnosis only.
                self._slow_down()
                log.debug(
                    "HTTP %d from %s (attempt %d/%d), retrying in %.1fs: %s",
                    response.status_code,
                    path,
                    attempt + 1,
                    MAX_ATTEMPTS,
                    self._interval,
                    response_headers(response),
                )
                self._sleep(self._interval)
                continue
            if response.is_error:
                # The status alone is in the exception; the body says whether
                # the query was rejected or the service is unwell, and the
                # headers record what the server offered.
                log.warning(
                    "HTTP %d from %s: %s\n%s",
                    response.status_code,
                    path,
                    response.text[:200],
                    response_headers(response),
                )
            else:
                self._speed_up()
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
        query = " AND ".join(clauses)
        params: dict[str, str | int] = {"query": query, "limit": limit}
        log.debug("search %r", query)
        payload = cast(ReleaseGroupSearch, self._get("/release-group", params))
        hits = payload.get("release-groups", [])
        log.debug(
            "search %r -> %d hits: %s",
            query,
            len(hits),
            [(hit.get("title"), hit.get("first-release-date")) for hit in hits],
        )
        return hits

    def release_group(self, mbid: str) -> ReleaseGroup:
        """One release group with its curated genres and community tags.

        `inc=genres` is an entity-lookup feature, which is why the search hit
        cannot stand in for this call. `artist-credit` would need its own `inc`
        term; the artist id for the fallback is taken from the search hit.
        """
        payload = cast(ReleaseGroup, self._get(f"/release-group/{mbid}", {"inc": "genres+tags"}))
        return payload

    def artist(self, mbid: str) -> Artist:
        """One artist's genres, used when a release group has none of its own."""
        payload = cast(Artist, self._get(f"/artist/{mbid}", {"inc": "genres"}))
        return payload
