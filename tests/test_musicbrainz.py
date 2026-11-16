import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

from arthouse.musicbrainz import MusicBrainzClient

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"


class ProbeClient(MusicBrainzClient):
    """Test seam: exercise the private transport call without file-wide ignores."""

    def request(self, path: str, params: dict[str, str | int] | None = None) -> object:
        return self._get(path, params)


class FakeClock:
    """Deterministic clock whose sleep advances time instead of blocking."""

    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock | None = None
) -> ProbeClient:
    clock = clock or FakeClock()
    return ProbeClient(
        "test-agent/1.0 ( test@example.com )",
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )


def _fixture(name: str) -> object:
    """A recorded MusicBrainz response, replayed verbatim."""
    return cast(object, json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def test_client_sends_contact_user_agent_and_json_accept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ws/2/release-group"
        assert request.url.params["query"] == 'releasegroup:"Kind of Blue"'
        assert request.url.params["fmt"] == "json"
        assert request.headers["User-Agent"] == "test-agent/1.0 ( test@example.com )"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        assert client.request("/release-group", {"query": 'releasegroup:"Kind of Blue"'}) == {
            "ok": True
        }


def test_client_enforces_one_request_per_second() -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler, clock) as client:
        client.request("/release-group")
        client.request("/release-group")

    assert clock.sleeps == [1.0]


def test_client_retries_a_503_then_succeeds() -> None:
    clock = FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    with _client(handler, clock) as client:
        assert client.request("/release-group") == {"ok": True}

    assert calls == 2
    assert clock.sleeps == [1.0]


def test_client_honours_retry_after() -> None:
    clock = FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, text="busy")
        return httpx.Response(200, json={"ok": True})

    with _client(handler, clock) as client:
        assert client.request("/release-group") == {"ok": True}

    assert clock.sleeps == [2.0]


def test_client_does_not_retry_a_400() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.request("/release-group")

    assert calls == 1


def test_client_gives_up_after_four_attempts() -> None:
    clock = FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="busy")

    with _client(handler, clock) as client, pytest.raises(httpx.HTTPStatusError):
        client.request("/release-group")

    assert calls == 4
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_search_release_groups_sends_a_title_and_artist_query() -> None:
    payload = _fixture("release-group-search.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ws/2/release-group"
        assert request.url.params["query"] == 'releasegroup:"Kind of Blue" AND artist:"Miles Davis"'
        assert request.url.params["limit"] == "5"
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        hits = client.search_release_groups("Kind of Blue", "Miles Davis")

    assert len(hits) == 5
    first = hits[0]
    assert first["title"] == "Kind of Blue"
    assert first.get("first-release-date") == "1959-08-17"
    # Search results carry community tags, never curated genres: `inc=genres`
    # is an entity-lookup feature, so the enrichment pass has to do the detail
    # call before it can read genres.
    assert "genres" not in first
    tags = {tag["name"]: tag["count"] for tag in first.get("tags", [])}
    assert tags["jazz"] == 28
    # The search index keeps net-rejected tags; the mapping layer drops these.
    assert tags["jazz and blues"] == 0


def test_search_release_groups_escapes_quotes_in_the_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == 'releasegroup:"\\"Heroes\\"" AND artist:"David Bowie"'
        return httpx.Response(200, json={})

    with _client(handler) as client:
        assert client.search_release_groups('"Heroes"', "David Bowie") == []


def test_release_group_detail_requests_genres_and_tags() -> None:
    payload = _fixture("release-group.json")
    mbid = "8e8a594f-2175-38c7-a871-abb68ec363e7"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/ws/2/release-group/{mbid}"
        assert request.url.params["inc"] == "genres+tags"
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        group = client.release_group(mbid)

    assert group["title"] == "Kind of Blue"
    assert "jazz" in {genre["name"] for genre in group.get("genres", [])}
    assert "jazz" in {tag["name"] for tag in group.get("tags", [])}
    # Pinned on purpose: with `inc=genres+tags` the detail payload has no
    # artist credit, so the artist id for the fallback must come from the
    # search hit rather than from this call.
    assert "artist-credit" not in group


def test_artist_detail_requests_genres_and_tags() -> None:
    payload = _fixture("artist.json")
    mbid = "561d854a-6a28-4aa7-8c99-323e6ce46c2a"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/ws/2/artist/{mbid}"
        assert request.url.params["inc"] == "genres+tags"
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        artist = client.artist(mbid)

    assert artist.get("name") == "Miles Davis"
    assert "jazz" in {genre["name"] for genre in artist.get("genres", [])}
    assert "jazz" in {tag["name"] for tag in artist.get("tags", [])}


def test_query_methods_share_the_one_request_per_second_throttle() -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler, clock) as client:
        client.search_release_groups("Kind of Blue", "Miles Davis")
        client.release_group("8e8a594f-2175-38c7-a871-abb68ec363e7")

    assert clock.sleeps == [1.0]


def test_search_tolerates_an_empty_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler) as client:
        assert client.search_release_groups("Nothing") == []
