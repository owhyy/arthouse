import httpx


async def test_index_renders_blank_page_with_navbar(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<nav" in response.text
    assert "/library" in response.text  # url_for emits absolute URLs
    assert "<script" not in response.text.lower()


async def test_unknown_route_404s(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404


async def test_security_headers_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
