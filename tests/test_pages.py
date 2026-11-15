import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from arthouse.config import BASE_DIR


def test_app_uses_a_throwaway_database(
    test_app: FastAPI, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The suite must not build or rebuild the real `data/library.db`."""
    # app.state is dynamically typed by Starlette.
    engine = cast(Engine, test_app.state.engine)
    db_path = Path(engine.url.database or "")
    assert db_path != BASE_DIR / "data" / "library.db"
    assert db_path.is_relative_to(tmp_path_factory.getbasetemp())


def test_importing_the_app_module_does_not_build_a_database(tmp_path: Path) -> None:
    """Building the DB happens in the lifespan, so import stays side-effect free."""
    env = {**os.environ, "ARTHOUSE_DATA_DIR": str(tmp_path)}
    subprocess.run(
        [sys.executable, "-c", "import arthouse.main"],
        cwd=BASE_DIR,
        env=env,
        check=True,
        capture_output=True,
    )
    assert not (tmp_path / "library.db").exists()


async def test_index_renders_blank_page_with_navbar(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<nav" in response.text
    assert "/library" in response.text  # url_for emits absolute URLs
    assert "htmx.min.js" in response.text  # interactive tabs ship with htmx
    assert "defer" in response.text


async def test_unknown_route_404s(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404


async def test_security_headers_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
