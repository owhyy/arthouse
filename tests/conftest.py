from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine

from arthouse.config import Settings
from arthouse.library import build_db
from arthouse.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def library_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    build_db(engine, FIXTURES)
    return engine


@pytest.fixture(scope="session")
def test_app(tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    """App on a throwaway data dir; the real `data/library.db` is never touched."""
    # debug=False keeps the tests independent of the developer's .env.
    return create_app(Settings(data_dir=tmp_path_factory.mktemp("data"), debug=False))


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with the real lifespan running; no network, no sockets."""
    async with test_app.router.lifespan_context(test_app):
        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    test_app.dependency_overrides.clear()
