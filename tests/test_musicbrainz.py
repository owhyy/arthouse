import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

from arthouse.musicbrainz import (
    MusicBrainzClient,
    ReleaseGroup,
    artist_queries,
    find_release_group,
    match_release_group,
)

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
    # The failure widens the throttle to 2s, which is also the retry wait.
    assert clock.sleeps == [2.0]


def test_client_ignores_a_retry_after_hint() -> None:
    clock = FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # Even a hint that would have mattered is not acted on: the throttle
        # is the only retry policy, because MusicBrainz sends a zero for its
        # busy 503 and its rate-limit headers describe a global bucket.
        return httpx.Response(503, headers={"Retry-After": "30"}, text="busy")

    with _client(handler, clock) as client, pytest.raises(httpx.HTTPStatusError):
        client.request("/release-group")

    assert calls == 4
    assert clock.sleeps == [2.0, 3.0, 4.0]


def test_client_recovers_the_throttle_after_a_success() -> None:
    clock = FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={})

    with _client(handler, clock) as client:
        client.request("/release-group")  # 503, retried, then a 200
        client.request("/release-group")  # back to the base interval

    # 2s for the retry, then the recovered 1s throttle for the next request.
    assert clock.sleeps == [2.0, 1.0]


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
    assert clock.sleeps == [2.0, 3.0, 4.0]


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


def test_search_logs_the_query_verbatim(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="arthouse.musicbrainz")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler) as client:
        client.search_release_groups("Either / Or", "Elliott Smith")

    messages = [record.getMessage() for record in caplog.records]
    text = "\n".join(messages)
    # The untransformed query is what explains a miss: a slash must not be
    # quietly rewritten into something the search engine cannot match.
    assert 'releasegroup:"Either / Or" AND artist:"Elliott Smith"' in text
    assert "-> 0 hits: []" in text


def test_client_logs_a_retry_with_its_status_wait_and_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="arthouse.musicbrainz")
    clock = FakeClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(200, json={"ok": True})

    with _client(handler, clock) as client:
        assert client.request("/release-group") == {"ok": True}

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "HTTP 429 from /release-group (attempt 1/4), retrying in 2.0s" in text
    assert "retry-after=2" in text


def test_client_logs_the_body_and_headers_of_a_hard_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="arthouse.musicbrainz")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"X-RateLimit-Remaining": "13"},
            text="bad release-group query",
        )

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.request("/release-group")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "HTTP 400 from /release-group: bad release-group query" in text
    # The headers are the only way to tell a retry hint from a hard refusal.
    assert "x-ratelimit-remaining=13" in text


def _hit(
    title: str,
    date: str | None = None,
    *,
    primary_type: str = "Album",
    mbid: str = "00000000-0000-0000-0000-000000000000",
) -> ReleaseGroup:
    """A search hit with only the fields the matcher reads."""
    hit: ReleaseGroup = {"id": mbid, "title": title}
    if date is not None:
        hit["first-release-date"] = date
    if primary_type:
        hit["primary-type"] = primary_type
    return hit


class StubClient:
    """Answers per artist clause, recording what the matcher asked for."""

    def __init__(self, responses: dict[str, list[ReleaseGroup]]) -> None:
        self.responses: dict[str, list[ReleaseGroup]] = responses
        self.queries: list[tuple[str, str]] = []

    def search_release_groups(
        self, title: str, artist: str = "", *, limit: int = 5
    ) -> list[ReleaseGroup]:
        self.queries.append((title, artist))
        return self.responses.get(artist, [])[:limit]


def test_match_release_group_accepts_an_exact_title() -> None:
    hits = [_hit("Kind of Blue", "1959-08-17")]
    assert match_release_group(hits, "Kind of Blue", 1959) is hits[0]


def test_match_release_group_rejects_a_near_miss() -> None:
    hits = [_hit("Kind of Blue / Blue Train", "1959")]
    assert match_release_group(hits, "Kind of Blue", 1959) is None


def test_match_release_group_unescapes_entities_on_both_sides() -> None:
    hits = [_hit("“Heroes”", "1977-10-14")]  # MusicBrainz uses curly quotes
    assert match_release_group(hits, "&#34;Heroes&#34;", 1977) is hits[0]


def test_match_release_group_prefers_an_album_over_a_same_year_single() -> None:
    single = _hit("Grace", "1994", primary_type="Single")
    album = _hit("Grace", "1994-08-15")
    assert match_release_group([single, album], "Grace", 1994) is album


def test_match_release_group_uses_the_year_as_a_tie_break() -> None:
    early = _hit("Kind of Blue", "1959")
    later = _hit("Kind of Blue", "1997")
    assert match_release_group([later, early], "Kind of Blue", 1958) is early


def test_match_release_group_reports_a_surviving_tie() -> None:
    first = _hit("Grace", "1994-08-15")
    second = _hit("Grace", "1994-11-01")
    assert match_release_group([first, second], "Grace", 1994) is None


def test_match_release_group_ignores_undated_candidates_when_a_year_is_known() -> None:
    undated = _hit("Grace")
    dated = _hit("Grace", "1994")
    assert match_release_group([undated, dated], "Grace", 1994) is dated


def test_match_release_group_reports_a_miss_when_nothing_is_dated() -> None:
    assert match_release_group([_hit("Grace")], "Grace", 1994) is None


def test_match_release_group_requires_uniqueness_without_a_year() -> None:
    hits = [_hit("Grace", "1994"), _hit("Grace", "2004")]
    assert match_release_group(hits, "Grace", None) is None
    assert match_release_group(hits[:1], "Grace", None) is hits[0]


def test_match_release_group_rejects_a_symbol_only_title() -> None:
    assert match_release_group([_hit("!!!")], "!!!", 2001) is None


def test_artist_queries_try_the_full_credit_before_splitting() -> None:
    assert artist_queries("Boldy James & Sterling Toles") == [
        "Boldy James & Sterling Toles",
        "Boldy James",
        "Sterling Toles",
    ]


def test_artist_queries_unescape_a_rym_credit() -> None:
    assert artist_queries("Boldy James &amp; Sterling Toles") == [
        "Boldy James & Sterling Toles",
        "Boldy James",
        "Sterling Toles",
    ]


def test_artist_queries_keep_a_band_name_intact_first() -> None:
    assert artist_queries("Black Country, New Road") == [
        "Black Country, New Road",
        "Black Country",
        "New Road",
    ]


def test_artist_queries_split_a_featuring_credit() -> None:
    assert artist_queries("Stan Getz & João Gilberto featuring Antônio Carlos Jobim") == [
        "Stan Getz & João Gilberto featuring Antônio Carlos Jobim",
        "Stan Getz",
        "João Gilberto",
        "Antônio Carlos Jobim",
    ]


def test_find_release_group_stops_at_the_first_match() -> None:
    album = _hit("Grace", "1994")
    client = StubClient({"Jeff Buckley": [album]})
    assert find_release_group(client, "Grace", "Jeff Buckley", 1994) is album
    assert client.queries == [("Grace", "Jeff Buckley")]


def test_find_release_group_falls_back_to_the_lead_artist() -> None:
    album = _hit("Manger on McNichols", "2020")
    client = StubClient({"Boldy James": [album]})
    found = find_release_group(
        client, "Manger on McNichols", "Boldy James &amp; Sterling Toles", 2020
    )
    assert found is album
    assert client.queries == [
        ("Manger on McNichols", "Boldy James & Sterling Toles"),
        ("Manger on McNichols", "Boldy James"),
    ]


def test_find_release_group_reports_a_miss() -> None:
    assert find_release_group(StubClient({}), "Grace", "Nobody", 1994) is None


def test_match_release_group_picks_the_album_from_a_recorded_search() -> None:
    payload = _fixture("release-group-search-grace.json")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        hits = client.search_release_groups("Grace", "Jeff Buckley")

    match = match_release_group(hits, "Grace", 1994)
    assert match is not None
    assert match.get("primary-type") == "Album"
    assert match.get("first-release-date") == "1994-08-15"


def test_match_release_group_unescapes_a_recorded_search() -> None:
    payload = _fixture("release-group-search-heroes.json")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        hits = client.search_release_groups("Heroes", "David Bowie")

    match = match_release_group(hits, "&#34;Heroes&#34;", 1977)
    assert match is not None
    assert match.get("primary-type") == "Album"
    assert match.get("first-release-date") == "1977-10-14"


def test_find_release_group_unescapes_the_title_before_searching() -> None:
    payload = _fixture("release-group-search-heroes.json")

    def handler(request: httpx.Request) -> httpx.Response:
        # The stored title is "&#34;Heroes&#34;"; the query must not carry it.
        assert request.url.params["query"] == 'releasegroup:"\\"Heroes\\"" AND artist:"David Bowie"'
        return httpx.Response(200, json=payload)

    with _client(handler) as client:
        group = find_release_group(client, "&#34;Heroes&#34;", "David Bowie", 1977)

    assert group is not None
    assert group.get("primary-type") == "Album"
    assert group.get("first-release-date") == "1977-10-14"
