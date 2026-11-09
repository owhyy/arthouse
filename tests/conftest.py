from collections.abc import AsyncIterator

import httpx
import pytest

from arthouse.main import app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with the real lifespan running; no network, no sockets."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()
