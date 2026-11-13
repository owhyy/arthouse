import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from arthouse.deps import get_db
from arthouse.descriptors import rebuild_descriptors
from arthouse.library import (
    FilmMeta,
    ItemTag,
    build_db,
    query_item,
    query_items,
)
from arthouse.main import app
from arthouse.routes import pages


def _enrich_film(
    engine: Engine,
    source_id: str,
    overview: str,
    genres: list[str],
    director: str | None = "Fixture Director",
    keywords: list[str] | None = None,
) -> None:
    with Session(engine) as db:
        db.add(
            FilmMeta(
                source="letterboxd",
                source_id=source_id,
                tmdb_id=1,
                overview=overview,
                poster_path=None,
                director=director,
                matched_title="Fixture Film A",
                matched_year=1999,
            )
        )
        for tag in genres:
            db.add(
                ItemTag(
                    source="letterboxd",
                    source_id=source_id,
                    provider="tmdb",
                    origin="genre",
                    tag=tag,
                )
            )
        for keyword in keywords or []:
            db.add(
                ItemTag(
                    source="letterboxd",
                    source_id=source_id,
                    provider="tmdb",
                    origin="keyword",
                    tag=keyword,
                )
            )
        db.commit()
    rebuild_descriptors(engine)


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


async def test_detail_page_shows_synopsis_and_genres(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _enrich_film(
        library_db,
        "https://boxd.it/aaa",
        "A fixture synopsis.",
        ["Drama", "Fantasy"],
        keywords=["slow cinema", "loneliness"],
    )
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film A")
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Synopsis" in response.text
    assert "A fixture synopsis." in response.text
    assert "Synopsis via TMDB" in response.text
    assert "Drama" in response.text
    assert "Fantasy" in response.text
    assert "Fixture Director" in response.text  # film credit line
    assert "Descriptors" in response.text
    assert "slow cinema" in response.text
    assert "loneliness" in response.text
    assert "descriptor=slow%20cinema" in response.text  # chips filter the library
    assert "descriptor=loneliness" in response.text


async def test_detail_page_omits_synopsis_when_unenriched(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film B")
    _override_db(library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Synopsis" not in response.text
    assert "Descriptors" not in response.text


async def test_film_grid_shows_genre_tags(client: httpx.AsyncClient, library_db: Engine) -> None:
    _enrich_film(library_db, "https://boxd.it/aaa", "A fixture synopsis.", ["Drama"])
    _override_db(library_db)
    try:
        response = await client.get("/library?tab=film")
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Drama" in response.text
    assert "Fixture Director" in response.text  # director stands in for creator
    assert "A fixture synopsis." not in response.text  # synopsis is detail-only


async def test_library_filters_films_by_descriptor(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _enrich_film(
        library_db,
        "https://boxd.it/aaa",
        "A fixture synopsis.",
        ["Drama"],
        keywords=["neo-noir", "melancholy"],
    )
    _enrich_film(
        library_db,
        "https://boxd.it/bbb",
        "Another synopsis.",
        ["Drama"],
        keywords=["neo-noir"],
    )
    _override_db(library_db)
    try:
        shared = await client.get("/library", params={"tab": "film", "descriptor": "neo-noir"})
        unique = await client.get("/library", params={"tab": "film", "descriptor": "melancholy"})
        combined = await client.get(
            "/library",
            params={"tab": "film", "q": "film b", "descriptor": "neo-noir"},
        )
    finally:
        _clear_override()
    assert shared.status_code == 200
    assert "Fixture Film A" in shared.text
    assert "Fixture Film B" in shared.text
    assert 'id="descriptor-filter"' in shared.text
    assert "neo-noir (2)" in shared.text
    assert unique.status_code == 200
    assert "Fixture Film A" in unique.text
    assert "Fixture Film B" not in unique.text
    # The active tab counts what is on screen; other tabs keep library totals.
    assert re.search(r"Films\s*<span[^>]*>1</span>", unique.text)
    assert re.search(r"Music\s*<span[^>]*>2</span>", unique.text)
    assert combined.status_code == 200
    assert "Fixture Film B" in combined.text
    assert "Fixture Film A" not in combined.text
    assert re.search(r"Films\s*<span[^>]*>1</span>", combined.text)


async def test_descriptor_filter_is_film_only(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _override_db(library_db)
    try:
        response = await client.get("/library", params={"tab": "music", "descriptor": "neo-noir"})
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert 'id="descriptor-filter"' not in response.text


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


def test_build_db_drops_legacy_enrichment(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    with engine.begin() as conn:
        for table in ("film_meta", "film_genre", "film_keyword"):
            conn.execute(
                text(
                    f"CREATE TABLE {table} (source VARCHAR NOT NULL, "
                    + "source_id VARCHAR NOT NULL, tag VARCHAR)"
                )
            )
        conn.execute(
            text("INSERT INTO film_genre VALUES ('letterboxd', 'https://boxd.it/old', 'Drama')")
        )
    build_db(engine, tmp_path / "nope")  # startup drops the pre-refactor tables
    inspector = inspect(engine)
    assert not inspector.has_table("film_meta")
    assert not inspector.has_table("film_genre")
    assert not inspector.has_table("film_keyword")
    with Session(engine) as db:
        # Legacy rows are not migrated; enrichment must be re-run.
        assert db.scalar(select(func.count()).select_from(ItemTag)) == 0
    build_db(engine, tmp_path / "nope")  # a second startup keeps the new tables
    assert inspect(engine).has_table("film_meta")


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


async def test_library_paginates_the_grid(
    client: httpx.AsyncClient, library_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pages, "PAGE_SIZE", 1)
    _override_db(library_db)
    try:
        first = await client.get("/library", params={"tab": "film", "page": 1})
        second = await client.get("/library", params={"tab": "film", "page": 2})
        clamped = await client.get("/library", params={"tab": "film", "page": 99})
        low = await client.get("/library", params={"tab": "film", "page": 0})
    finally:
        _clear_override()
    titles = ("Fixture Film A", "Fixture Film B")
    first_titles = {title for title in titles if title in first.text}
    second_titles = {title for title in titles if title in second.text}
    assert len(first_titles) == 1
    assert len(second_titles) == 1
    assert first_titles != second_titles
    assert "Page 1 of 2" in first.text
    assert "Page 2 of 2" in second.text
    assert "Page 2 of 2" in clamped.text  # out-of-range pages clamp to the last page
    assert "Page 1 of 2" in low.text


async def test_pagination_preserves_the_descriptor_filter(
    client: httpx.AsyncClient, library_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enrich_film(library_db, "https://boxd.it/aaa", "A.", ["Drama"], keywords=["neo-noir"])
    _enrich_film(library_db, "https://boxd.it/bbb", "B.", ["Drama"], keywords=["neo-noir"])
    monkeypatch.setattr(pages, "PAGE_SIZE", 1)
    _override_db(library_db)
    try:
        response = await client.get("/library", params={"tab": "film", "descriptor": "neo-noir"})
    finally:
        _clear_override()
    assert response.status_code == 200
    assert "Page 1 of 2" in response.text
    assert "&amp;page=2&amp;descriptor=neo-noir" in response.text


async def test_search_filters_titles_in_the_active_tab(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _override_db(library_db)
    try:
        films = await client.get("/library", params={"tab": "film", "q": "film a"})
        music = await client.get("/library", params={"tab": "music", "q": "film a"})
    finally:
        _clear_override()
    assert films.status_code == 200
    assert "Fixture Film A" in films.text
    assert "Fixture Film B" not in films.text
    assert re.search(r"Films\s*<span[^>]*>1</span>", films.text)
    assert "No titles match" in music.text  # search does not cross tabs


async def test_search_is_case_insensitive(client: httpx.AsyncClient, library_db: Engine) -> None:
    _override_db(library_db)
    try:
        response = await client.get("/library", params={"tab": "film", "q": "FIXTURE FILM B"})
    finally:
        _clear_override()
    assert "Fixture Film B" in response.text
    assert "Fixture Film A" not in response.text


async def test_search_matches_titles_only(client: httpx.AsyncClient, library_db: Engine) -> None:
    _override_db(library_db)
    try:
        # "Fixture Artist" is a creator, not a title; review text is ignored too.
        creators = await client.get("/library", params={"tab": "music", "q": "fixture artist"})
        reviews = await client.get("/library", params={"tab": "book", "q": "markup"})
    finally:
        _clear_override()
    assert "No titles match" in creators.text
    assert "No titles match" in reviews.text


async def test_search_keeps_query_in_tab_links(
    client: httpx.AsyncClient, library_db: Engine
) -> None:
    _override_db(library_db)
    try:
        response = await client.get("/library", params={"tab": "film", "q": "fixture"})
    finally:
        _clear_override()
    assert "q=fixture" in response.text  # switching tabs keeps the search
    assert 'name="q"' in response.text  # the box lives outside the swapped region


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
