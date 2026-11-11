from collections.abc import Iterator
from pathlib import Path

import httpx
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from arthouse.deps import get_db
from arthouse.library import build_db, query_item, query_items
from arthouse.main import app


def _override_db(engine: Engine) -> None:
    def session_factory() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = session_factory


def _clear_override() -> None:
    app.dependency_overrides.pop(get_db)


def test_build_db_parses_all_platforms(library_db: Engine) -> None:
    with Session(library_db) as db:
        assert len(query_items(db)) == 5


def test_rym_ratings_are_normalized(library_db: Engine) -> None:
    with Session(library_db) as db:
        albums = {i.title: i for i in query_items(db, media_type="music")}
    assert albums["Fixture Best Album"].rating == 5.0
    assert albums["Fixture Album"].rating == 4.0
    assert albums["Fixture Album"].rating_native == "8/10"
    assert albums["Fixture Album"].creator == "Fixture Artist"


def test_unrated_and_wishlist_items_are_skipped(library_db: Engine) -> None:
    with Session(library_db) as db:
        titles = {i.title for i in query_items(db)}
    assert "Fixture Unrated" not in titles  # RYM rating 0
    assert "Fixture Book B" not in titles  # Goodreads read but unrated
    assert "Fixture Book C" not in titles  # to-read shelf, never consumed


def test_query_sorts_by_rating_desc(library_db: Engine) -> None:
    with Session(library_db) as db:
        ratings = [i.rating for i in query_items(db)]
    assert ratings == sorted(ratings, reverse=True)


def test_query_filters_by_media_type(library_db: Engine) -> None:
    with Session(library_db) as db:
        assert len(query_items(db, media_type="film")) == 2
        assert len(query_items(db, media_type="book")) == 1


def test_scores_normalize_to_10_point_scale(library_db: Engine) -> None:
    with Session(library_db) as db:
        items = {i.title: i for i in query_items(db)}
    # RYM 8/10 -> 8, Letterboxd 4.5 stars -> 9, Goodreads 4.0 -> 8.
    assert items["Fixture Album"].score == "8"
    assert items["Fixture Film A"].score == "9"
    assert items["Fixture Book A"].score == "8"
    assert items["Fixture Book A"].rating_native == "4.0"


def test_reviews_are_parsed_and_cleaned(library_db: Engine) -> None:
    with Session(library_db) as db:
        items = {i.title: i for i in query_items(db)}
    # Letterboxd review text lives in reviews.csv and its export URIs differ
    # from ratings.csv, so it joins on title + year. HTML is stripped.
    assert items["Fixture Film A"].review == "A fixture review.\nSecond line."
    assert items["Fixture Album"].review == "Fixture review text."
    assert items["Fixture Book A"].review == "Book review with markup."
    assert items["Fixture Best Album"].review is None


async def test_detail_page_shows_review(client: httpx.AsyncClient, library_db: Engine) -> None:
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film A")
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Review" in response.text
    assert "A fixture review." in response.text
    assert "Second line." in response.text
    assert "<b>fixture</b>" not in response.text  # cleaned, not raw HTML


async def test_detail_page_omits_empty_review(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    with Session(library_db) as db:
        album = next(i for i in query_items(db) if i.title == "Fixture Best Album")
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{album.id}")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Review" not in response.text


async def test_detail_page_shows_provenance(client: httpx.AsyncClient, library_db: Engine) -> None:
    with Session(library_db) as db:
        album = next(i for i in query_items(db) if i.title == "Fixture Album")
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{album.id}")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert "rateyourmusic" in response.text  # platform
    assert "8/10" in response.text  # native rating (RYM: unchanged by x2)
    assert "Rated on" in response.text
    assert "—" in response.text  # RYM exports carry no rating date


async def test_detail_page_converts_native_rating(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film A")
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "4.5" in response.text  # native stars
    assert "9/10" in response.text  # normalized
    assert "2026-02-10" in response.text  # rating date


async def test_unknown_item_404s(client: httpx.AsyncClient, library_db: Engine) -> None:
    with Session(library_db) as db:
        known = query_item(db, 1)
    assert known is not None
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{known.id + 999}")
    finally:
        _clear_override()
    assert response.status_code == 404


def test_build_db_tolerates_missing_data_dir(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    assert build_db(engine, tmp_path / "nope") == 0


async def test_library_page_shows_default_music_tab(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _override_db(library_db)
    try:
        response = await client.get("/library")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert "Fixture Film A" not in response.text  # only the active tab renders
    for label in ("Music", "Films", "Books"):
        assert label in response.text  # tab strip


async def test_library_tab_param_switches_category(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _override_db(library_db)
    try:
        films = await client.get("/library?tab=film")
        books = await client.get("/library?tab=book")
        bogus = await client.get("/library?tab=bogus")
    finally:
        _clear_override()
    assert films.status_code == 200
    assert "Fixture Film A" in films.text
    assert "Fixture Album" not in films.text
    assert books.status_code == 200
    assert "Fixture Book A" in books.text
    assert bogus.status_code == 404


async def test_library_tabs_use_htmx_swap(client: httpx.AsyncClient, library_db: Engine) -> None:
    _override_db(library_db)
    try:
        response = await client.get("/library")
    finally:
        _clear_override()
    assert 'hx-target="#library-region"' in response.text
    assert 'hx-select="#library-region"' in response.text
    assert 'hx-push-url="true"' in response.text
    assert 'id="library-region"' in response.text


async def test_library_page_empty_when_no_data(client: httpx.AsyncClient, tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    build_db(engine, tmp_path / "nope")
    _override_db(engine)
    try:
        response = await client.get("/library")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "0 rated items" in response.text
    assert "Nothing here yet." in response.text
