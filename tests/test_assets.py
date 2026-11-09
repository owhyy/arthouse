import hashlib
import re

import httpx
import pytest

from arthouse.assets import asset_url, build_asset_map
from arthouse.config import BASE_DIR

STATIC_DIR = BASE_DIR / "static"


def test_build_asset_map_hashes_all_static_files() -> None:
    asset_map = build_asset_map(STATIC_DIR)
    assert "css/app.css" in asset_map
    assert (
        asset_map["css/app.css"]
        == (hashlib.sha256((STATIC_DIR / "css/app.css").read_bytes()).hexdigest()[:10])
    )


def test_asset_url_is_versioned() -> None:
    asset_map = build_asset_map(STATIC_DIR)
    assert asset_url(STATIC_DIR, asset_map, "css/app.css") == (
        f"/static/css/app.css?v={asset_map['css/app.css']}"
    )


def test_asset_url_strips_leading_slash() -> None:
    asset_map = build_asset_map(STATIC_DIR)
    assert asset_url(STATIC_DIR, asset_map, "/css/app.css") == asset_url(
        STATIC_DIR, asset_map, "css/app.css"
    )


def test_asset_url_unknown_path_falls_back_unversioned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        url = asset_url(STATIC_DIR, {"css/app.css": "abc"}, "css/nope.css")
    assert url == "/static/css/nope.css"
    assert "unknown static path" in caplog.text


def test_asset_url_unknown_path_raises_in_debug() -> None:
    with pytest.raises(RuntimeError, match="unknown static path"):
        asset_url(STATIC_DIR, {}, "css/nope.css", debug=True)


def test_asset_url_debug_hashes_live() -> None:
    asset_map = build_asset_map(STATIC_DIR)
    url = asset_url(STATIC_DIR, asset_map, "css/app.css", debug=True)
    assert url == asset_url(STATIC_DIR, asset_map, "css/app.css")


async def test_index_links_use_versioned_assets(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    for pattern, rel_path in ((r'href="/static/css/app\.css\?v=([0-9a-f]{10})"', "css/app.css"),):
        match = re.search(pattern, response.text)
        assert match, pattern
        expected = hashlib.sha256((STATIC_DIR / rel_path).read_bytes()).hexdigest()[:10]
        assert match.group(1) == expected
