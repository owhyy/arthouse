from collections.abc import Callable

import httpx
import pytest

from arthouse.musicbrainz import MusicBrainzClient


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
