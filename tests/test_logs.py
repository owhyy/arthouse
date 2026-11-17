"""Logging policy: one place decides the level and quiets the transport."""

import logging
from collections.abc import Iterator

import httpx
import pytest

from arthouse.config import Settings
from arthouse.logs import configure_logging, response_headers

NOISY = ("httpx", "httpcore")


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Levels are process-global; put them back so tests stay independent."""
    root = logging.getLogger()
    levels = {name: logging.getLogger(name).level for name in NOISY}
    previous_root = root.level
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    root.setLevel(previous_root)


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_quiets_the_transport_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arthouse.logs.get_settings", lambda: Settings(debug=True))

    configure_logging()

    # Our client already logs the request, status and retry; httpx's duplicate
    # request line and httpcore's byte trace are noise even at DEBUG.
    for name in NOISY:
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_uses_debug_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arthouse.logs.get_settings", lambda: Settings(debug=True))

    configure_logging()

    assert logging.getLogger().level == logging.DEBUG


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_stays_at_info_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arthouse.logs.get_settings", lambda: Settings(debug=False))

    configure_logging()

    assert logging.getLogger().level == logging.INFO


def test_response_headers_fit_on_one_sorted_line() -> None:
    response = httpx.Response(503, headers={"b": "2", "a": "1"})

    assert response_headers(response) == "a=1; b=2"
