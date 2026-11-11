from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine

from arthouse.library import build_db
from arthouse.main import app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def library_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    build_db(engine, FIXTURES)
    return engine


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with the real lifespan running; no network, no sockets."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()
