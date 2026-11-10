from pathlib import Path

import httpx
import pytest

from arthouse.deps import get_db_path
from arthouse.library import build_db, query_items
from arthouse.main import app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def library_db(tmp_path: Path) -> Path:
    db = tmp_path / "library.db"
    build_db(db, FIXTURES)
    return db


def test_build_db_parses_all_platforms(library_db: Path) -> None:
    assert len(query_items(library_db)) == 5


def test_rym_ratings_are_normalized(library_db: Path) -> None:
    albums = {i.title: i for i in query_items(library_db, media_type="music")}
    assert albums["Fixture Best Album"].rating == 5.0
    assert albums["Fixture Album"].rating == 4.0
    assert albums["Fixture Album"].rating_native == "8/10"
    assert albums["Fixture Album"].creator == "Fixture Artist"


def test_unrated_and_wishlist_items_are_skipped(library_db: Path) -> None:
    titles = {i.title for i in query_items(library_db)}
    assert "Fixture Unrated" not in titles  # RYM rating 0
    assert "Fixture Book B" not in titles  # Goodreads read but unrated
    assert "Fixture Book C" not in titles  # to-read shelf, never consumed


def test_query_sorts_by_rating_desc(library_db: Path) -> None:
    ratings = [i.rating for i in query_items(library_db)]
    assert ratings == sorted(ratings, reverse=True)


def test_query_filters_by_media_type(library_db: Path) -> None:
    assert len(query_items(library_db, media_type="film")) == 2
    assert len(query_items(library_db, media_type="book")) == 1


def test_build_db_tolerates_missing_data_dir(tmp_path: Path) -> None:
    assert build_db(tmp_path / "library.db", tmp_path / "nope") == 0


async def test_library_page_renders_groups(client: httpx.AsyncClient, library_db: Path) -> None:
    app.dependency_overrides[get_db_path] = lambda: library_db
    try:
        response = await client.get("/library")
    finally:
        app.dependency_overrides.pop(get_db_path)
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert "8/10" in response.text
    for label in ("Music", "Films", "Books"):
        assert label in response.text


async def test_library_page_empty_when_no_data(client: httpx.AsyncClient, tmp_path: Path) -> None:
    empty_db = tmp_path / "empty.db"
    build_db(empty_db, tmp_path / "nope")
    app.dependency_overrides[get_db_path] = lambda: empty_db
    try:
        response = await client.get("/library")
    finally:
        app.dependency_overrides.pop(get_db_path)
    assert response.status_code == 200
    assert "0 rated items" in response.text
