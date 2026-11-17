import csv
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from arthouse.db import create_db_engine
from arthouse.deps import get_db
from arthouse.library import build_db, normalize_title, split_genres
from arthouse.models import Base, GenreRow, ItemGenre, ItemMeta, ItemTag
from arthouse.queries import (
    count_items,
    count_items_by_media,
    query_item,
    query_item_credits,
    query_item_genres,
    query_items,
)
from arthouse.routes import pages
from arthouse.tagging import rebuild_descriptors


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
            ItemMeta(
                source="letterboxd",
                source_id=source_id,
                provider="tmdb",
                external_id="1",
                summary=overview,
                image_path=None,
                credit=director,
                matched_title="Fixture Film A",
                matched_year=1999,
            )
        )
        for tag in genres:
            db.add(
                ItemGenre(
                    source="letterboxd",
                    source_id=source_id,
                    provider="tmdb",
                    genre=tag,
                )
            )
        for keyword in keywords or []:
            db.add(
                ItemTag(
                    source="letterboxd",
                    source_id=source_id,
                    provider="tmdb",
                    tag=keyword,
                )
            )
        db.commit()
    rebuild_descriptors(engine)


def _override_db(app: FastAPI, engine: Engine) -> None:
    def session_factory() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = session_factory


def _clear_override(app: FastAPI) -> None:
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


def test_rym_html_entities_are_decoded(tmp_path: Path) -> None:
    """RYM escapes `&` and `"` in the name and title columns.

    The parser decodes them, so search, sorting and the templates all see the
    text a browser would show instead of a literal `&amp;`.
    """
    export = tmp_path / "rateyourmusic" / "export.csv"
    export.parent.mkdir()
    columns = (
        "RYM Album",
        "First Name",
        "Last Name",
        "First Name localized",
        " Last Name localized",  # the stray space is in the real header too
        "Title",
        "Release_Date",
        "Rating",
        "Ownership",
        "Purchase Date",
        "Media Type",
        " Review",
        " Review Title",
    )
    rows = (
        (
            "1",
            "Stan",
            "Getz &amp; João Gilberto featuring Antônio Carlos Jobim",
            "",
            "",
            "Getz / Gilberto",
            "1964",
            "10",
            "n",
            "",
            "",
            "",
            "",
        ),
        ("2", "David", "Bowie", "", "", "&#34;Heroes&#34;", "1977", "7", "n", "", "", "", ""),
    )
    with open(export, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    build_db(engine, tmp_path)
    with Session(engine) as db:
        albums = {item.title: item.creator for item in query_items(db, media_type="music")}
    getz = albums["Getz / Gilberto"]
    assert getz == "Stan Getz & João Gilberto featuring Antônio Carlos Jobim"
    assert albums['"Heroes"'] == "David Bowie"


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


def test_query_items_paginates_with_limit_and_offset(library_db: Engine) -> None:
    with Session(library_db) as db:
        all_albums = query_items(db, media_type="music")
        first = query_items(db, media_type="music", limit=1)
        second = query_items(db, media_type="music", limit=1, offset=1)
        assert count_items(db, media_type="music") == len(all_albums) == 2
    assert [album.id for album in first] == [all_albums[0].id]
    assert [album.id for album in second] == [all_albums[1].id]


def test_count_items_matches_the_filters(library_db: Engine) -> None:
    _enrich_film(library_db, "https://boxd.it/aaa", "A.", ["Drama"], keywords=["neo-noir"])
    with Session(library_db) as db:
        assert count_items(db, media_type="film") == 2
        assert count_items(db, media_type="film", search="film a") == 1
        assert count_items(db, media_type="film", descriptor="dark") == 1
        assert count_items(db, media_type="film", search="film a", descriptor="dark") == 1
        assert count_items(db, media_type="film", descriptor="unknown") == 0


def test_count_items_by_media_groups_the_tab_totals(library_db: Engine) -> None:
    with Session(library_db) as db:
        assert count_items_by_media(db) == {"music": 2, "film": 2, "book": 1}


def test_query_item_credits_are_scoped_to_the_medium(library_db: Engine) -> None:
    _enrich_film(library_db, "https://boxd.it/aaa", "A.", ["Drama"])
    with Session(library_db) as db:
        assert query_item_credits(db, "film") == {
            "https://boxd.it/aaa": "Fixture Director",
        }
        assert query_item_credits(db, "music") == {}


def test_query_item_genres_can_be_scoped_to_visible_ids(library_db: Engine) -> None:
    _add_music_genres(library_db, [("rock", 17)])
    with Session(library_db) as db:
        assert query_item_genres(db, "music") == {"1": [GenreRow("rock", 17)]}
        assert query_item_genres(db, "music", ["1"]) == {"1": [GenreRow("rock", 17)]}
        assert query_item_genres(db, "music", []) == {}
        assert query_item_genres(db, "music", ["missing"]) == {}


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


async def test_detail_page_shows_review(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film A")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Review" in response.text
    assert "A fixture review." in response.text
    assert "Second line." in response.text
    assert "<b>fixture</b>" not in response.text  # cleaned, not raw HTML


async def test_detail_page_shows_synopsis_and_genres(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _enrich_film(
        library_db,
        "https://boxd.it/aaa",
        "A fixture synopsis.",
        ["Drama", "Fantasy"],
        keywords=["loneliness", "melancholy"],
    )
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film A")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Synopsis" in response.text
    assert "A fixture synopsis." in response.text
    assert "Synopsis via TMDB" in response.text
    assert "Drama" in response.text
    assert "Fantasy" in response.text
    # TMDB genres have no vote tally, so they never split into a second tier.
    assert "More genres" not in response.text
    assert "Fixture Director" in response.text  # film credit line
    assert "Descriptors" in response.text
    assert "loneliness" in response.text
    assert "melancholic" in response.text  # "melancholy" resolves to the descriptor
    assert "melancholy" not in response.text
    assert "descriptor=loneliness" in response.text  # chips filter the library
    assert "descriptor=melancholic" in response.text


async def test_detail_page_omits_synopsis_when_unenriched(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film B")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Synopsis" not in response.text
    assert "Descriptors" not in response.text


async def test_film_grid_shows_genre_tags(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _enrich_film(library_db, "https://boxd.it/aaa", "A fixture synopsis.", ["Drama"])
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library?tab=film")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Drama" in response.text
    assert "Fixture Director" in response.text  # director stands in for creator
    assert "A fixture synopsis." not in response.text  # synopsis is detail-only


def _add_music_genres(
    engine: Engine, genres: list[tuple[str, int]], *, source_id: str = "1"
) -> None:
    """Give the fixture album metadata and voted genres, like the music pass does."""
    with Session(engine) as db:
        db.add(
            ItemMeta(
                source="rateyourmusic",
                source_id=source_id,
                provider="musicbrainz",
                external_id="mbid-1",
                summary="",
                image_path=None,
                credit="Fixture Artist",
                matched_title="Fixture Album",
                matched_year=2018,
            )
        )
        for name, count in genres:
            db.add(
                ItemGenre(
                    source="rateyourmusic",
                    source_id=source_id,
                    provider="musicbrainz",
                    genre=name,
                    count=count,
                )
            )
        db.commit()


async def test_music_detail_page_splits_primary_and_secondary_genres(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _add_music_genres(
        library_db,
        [
            ("rock", 17),
            ("folk rock", 7),
            ("pop rock", 7),
            ("pop", 6),
            ("beat music", 1),
        ],
    )
    with Session(library_db) as db:
        album = next(i for i in query_items(db) if i.title == "Fixture Album")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{album.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "More genres" in response.text
    # The voted head comes before the tail, and the tail is still shown.
    assert response.text.index("pop rock") < response.text.index("More genres")
    assert response.text.index("More genres") < response.text.index("beat music")
    assert "rock — 17 votes" in response.text


async def test_music_grid_ranks_genres_and_fades_the_overflow(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _add_music_genres(
        library_db,
        [("rock", 17), ("folk rock", 7), ("pop", 6), ("beat music", 1)],
    )
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library?tab=music")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "genre-strip" in response.text
    # The strip clips, so the title carries the full list in rank order.
    assert 'title="rock, folk rock, pop, beat music"' in response.text


def test_split_genres_keeps_the_voted_head_as_primary() -> None:
    genres = [
        GenreRow("rock", 17),
        GenreRow("folk rock", 7),
        GenreRow("pop rock", 7),
        GenreRow("pop", 6),
        GenreRow("beat music", 1),
    ]
    assert split_genres(genres) == (
        [GenreRow("rock", 17), GenreRow("folk rock", 7), GenreRow("pop rock", 7)],
        [GenreRow("pop", 6), GenreRow("beat music", 1)],
    )


def test_split_genres_keeps_everything_without_votes() -> None:
    # TMDB genres carry no tally, so the film display must not change.
    genres = [GenreRow("Drama", 0), GenreRow("Fantasy", 0)]
    assert split_genres(genres) == (genres, [])


def test_split_genres_treats_a_single_vote_as_no_signal() -> None:
    genres = [GenreRow("a", 1), GenreRow("b", 1)]
    assert split_genres(genres) == (genres, [])


def test_split_genres_caps_a_wide_primary_cluster() -> None:
    genres = [GenreRow(f"g{index}", 5) for index in range(6)]
    primary, secondary = split_genres(genres)
    assert len(primary) == 4
    assert secondary == [GenreRow("g4", 5), GenreRow("g5", 5)]


def test_split_genres_handles_an_empty_list() -> None:
    assert split_genres([]) == ([], [])


async def test_library_filters_films_by_descriptor(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
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
    _override_db(test_app, library_db)
    try:
        shared = await client.get("/library", params={"tab": "film", "descriptor": "neo-noir"})
        unique = await client.get("/library", params={"tab": "film", "descriptor": "melancholy"})
        combined = await client.get(
            "/library",
            params={"tab": "film", "q": "film b", "descriptor": "neo-noir"},
        )
    finally:
        _clear_override(test_app)
    assert shared.status_code == 200
    assert "Fixture Film A" in shared.text
    assert "Fixture Film B" in shared.text
    assert 'id="descriptor-filter"' in shared.text
    assert "dark (2)" in shared.text  # "neo-noir" is an alias of atmosphere/dark
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


async def test_descriptor_filter_applies_to_every_medium(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    """Descriptors are cross-media, so the filter is no longer film-only."""
    with Session(library_db) as db:
        db.add(
            ItemTag(
                source="rateyourmusic",
                source_id="1",
                provider="musicbrainz",
                tag="melancholy",
            )
        )
        db.commit()
    rebuild_descriptors(library_db)
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library", params={"tab": "music", "descriptor": "melancholy"})
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert "Fixture Best Album" not in response.text
    assert 'id="descriptor-filter"' in response.text
    assert "melancholic (1)" in response.text


async def test_detail_chips_link_to_the_items_own_tab(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    """Album chips must not link to `tab=film` (regression: hardcoded tab)."""
    with Session(library_db) as db:
        db.add(
            ItemTag(
                source="rateyourmusic",
                source_id="1",
                provider="musicbrainz",
                tag="melancholy",
            )
        )
        db.commit()
    rebuild_descriptors(library_db)
    with Session(library_db) as db:
        album = next(i for i in query_items(db) if i.title == "Fixture Album")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{album.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "tab=music&amp;descriptor=melancholic" in response.text


async def test_detail_page_omits_empty_review(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    with Session(library_db) as db:
        album = next(i for i in query_items(db) if i.title == "Fixture Best Album")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{album.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Review" not in response.text


async def test_detail_page_shows_provenance(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    with Session(library_db) as db:
        album = next(i for i in query_items(db) if i.title == "Fixture Album")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{album.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert "rateyourmusic" in response.text  # platform
    assert "8/10" in response.text  # native rating (RYM: unchanged by x2)
    assert "Rated on" in response.text
    assert "—" in response.text  # RYM exports carry no rating date


async def test_detail_page_converts_native_rating(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    with Session(library_db) as db:
        film = next(i for i in query_items(db) if i.title == "Fixture Film A")
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{film.id}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "4.5" in response.text  # native stars
    assert "9/10" in response.text  # normalized
    assert "2026-02-10" in response.text  # rating date


async def test_unknown_item_404s(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    with Session(library_db) as db:
        known = query_item(db, 1)
    assert known is not None
    _override_db(test_app, library_db)
    try:
        response = await client.get(f"/library/{known.id + 999}")
    finally:
        _clear_override(test_app)
    assert response.status_code == 404


def test_genres_never_feed_the_vocabulary(library_db: Engine) -> None:
    with Session(library_db) as db:
        for name, artist_level in (("Drama", False), ("art pop", True)):
            db.add(
                ItemGenre(
                    source="letterboxd",
                    source_id="https://boxd.it/aaa",
                    provider="tmdb",
                    genre=name,
                    artist_level=artist_level,
                )
            )
        db.add(
            ItemTag(
                source="letterboxd",
                source_id="https://boxd.it/aaa",
                provider="tmdb",
                tag="melancholy",
            )
        )
        db.commit()

    stats = rebuild_descriptors(library_db)

    assert stats.tags == 1
    assert stats.mapped == 1
    assert stats.pairs == 1


def test_build_db_tolerates_missing_data_dir(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    assert build_db(engine, tmp_path / "nope") == 0


def test_library_tables_have_the_scale_indexes(library_db: Engine) -> None:
    inspector = inspect(library_db)
    assert {"ix_items_identity", "ix_items_media_rating_title"} <= {
        index["name"] for index in inspector.get_indexes("items")
    }
    assert "ix_item_meta_provider" in {
        index["name"] for index in inspector.get_indexes("item_meta")
    }
    assert "ix_item_descriptor_name" in {
        index["name"] for index in inspector.get_indexes("item_descriptor")
    }


def test_build_db_adds_indexes_missing_from_an_existing_table(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_item_meta_provider"))
    assert "ix_item_meta_provider" not in {
        index["name"] for index in inspect(engine).get_indexes("item_meta")
    }
    build_db(engine, tmp_path / "nope")
    assert "ix_item_meta_provider" in {
        index["name"] for index in inspect(engine).get_indexes("item_meta")
    }


def test_db_engine_enables_wal_and_waits_instead_of_failing(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "library.db")
    with engine.connect() as conn:
        assert cast(str, conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()) == "wal"
        # NORMAL is 1; WAL does not need the default FULL fsync on every commit.
        assert cast(int, conn.exec_driver_sql("PRAGMA synchronous").scalar_one()) == 1
        assert cast(int, conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()) == 5000
        assert cast(int, conn.exec_driver_sql("PRAGMA cache_size").scalar_one()) == -16000
    engine.dispose()


async def test_library_page_shows_default_music_tab(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Fixture Album" in response.text
    assert "Fixture Film A" not in response.text  # only the active tab renders
    for label in ("Music", "Films", "Books"):
        assert label in response.text  # tab strip


async def test_library_tab_param_switches_category(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        films = await client.get("/library?tab=film")
        books = await client.get("/library?tab=book")
        bogus = await client.get("/library?tab=bogus")
    finally:
        _clear_override(test_app)
    assert films.status_code == 200
    assert "Fixture Film A" in films.text
    assert "Fixture Album" not in films.text
    assert books.status_code == 200
    assert "Fixture Book A" in books.text
    assert bogus.status_code == 404


async def test_library_tabs_use_htmx_swap(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library")
    finally:
        _clear_override(test_app)
    assert 'hx-target="#library-region"' in response.text
    assert 'hx-select="#library-region"' in response.text
    assert 'hx-push-url="true"' in response.text
    assert 'id="library-region"' in response.text


async def test_oob_hidden_inputs_keep_their_target_ids(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        response = await client.get(
            "/library", params={"tab": "book"}, headers={"HX-Request": "true"}
        )
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    # htmx replaces the target with a clone of the OOB element, so the element
    # must carry the same id or the next swap silently misses (the search form
    # then submits a stale tab/descriptor).
    oob_tags: list[str] = re.findall(r"<input[^>]*hx-swap-oob[^>]*>", response.text)
    assert len(oob_tags) == 2
    replaced = {}
    for tag in oob_tags:
        tag_id = re.search(r'id="([^"]+)"', tag)
        target = re.search(r'hx-swap-oob="outerHTML:#([^"]+)"', tag)
        assert tag_id is not None
        assert target is not None
        replaced[target.group(1)] = (tag_id.group(1), tag)
    assert replaced["library-tab"][0] == "library-tab"
    assert replaced["library-descriptor"][0] == "library-descriptor"
    assert 'value="book"' in replaced["library-tab"][1]


async def test_library_paginates_the_grid(
    client: httpx.AsyncClient,
    library_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    test_app: FastAPI,
) -> None:
    monkeypatch.setattr(pages, "PAGE_SIZE", 1)
    _override_db(test_app, library_db)
    try:
        first = await client.get("/library", params={"tab": "film", "page": 1})
        second = await client.get("/library", params={"tab": "film", "page": 2})
        clamped = await client.get("/library", params={"tab": "film", "page": 99})
        low = await client.get("/library", params={"tab": "film", "page": 0})
    finally:
        _clear_override(test_app)
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
    client: httpx.AsyncClient,
    library_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    test_app: FastAPI,
) -> None:
    _enrich_film(library_db, "https://boxd.it/aaa", "A.", ["Drama"], keywords=["neo-noir"])
    _enrich_film(library_db, "https://boxd.it/bbb", "B.", ["Drama"], keywords=["neo-noir"])
    monkeypatch.setattr(pages, "PAGE_SIZE", 1)
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library", params={"tab": "film", "descriptor": "neo-noir"})
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "Page 1 of 2" in response.text
    assert "&amp;page=2&amp;descriptor=dark" in response.text


async def test_search_filters_titles_in_the_active_tab(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        films = await client.get("/library", params={"tab": "film", "q": "film a"})
        music = await client.get("/library", params={"tab": "music", "q": "film a"})
    finally:
        _clear_override(test_app)
    assert films.status_code == 200
    assert "Fixture Film A" in films.text
    assert "Fixture Film B" not in films.text
    assert re.search(r"Films\s*<span[^>]*>1</span>", films.text)
    assert "No titles match" in music.text  # search does not cross tabs


async def test_search_is_case_insensitive(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library", params={"tab": "film", "q": "FIXTURE FILM B"})
    finally:
        _clear_override(test_app)
    assert "Fixture Film B" in response.text
    assert "Fixture Film A" not in response.text


async def test_search_matches_titles_only(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        # "Fixture Artist" is a creator, not a title; review text is ignored too.
        creators = await client.get("/library", params={"tab": "music", "q": "fixture artist"})
        reviews = await client.get("/library", params={"tab": "book", "q": "markup"})
    finally:
        _clear_override(test_app)
    assert "No titles match" in creators.text
    assert "No titles match" in reviews.text


async def test_search_keeps_query_in_tab_links(
    client: httpx.AsyncClient, library_db: Engine, test_app: FastAPI
) -> None:
    _override_db(test_app, library_db)
    try:
        response = await client.get("/library", params={"tab": "film", "q": "fixture"})
    finally:
        _clear_override(test_app)
    assert "q=fixture" in response.text  # switching tabs keeps the search
    assert 'name="q"' in response.text  # the box lives outside the swapped region


async def test_library_page_empty_when_no_data(
    client: httpx.AsyncClient, tmp_path: Path, test_app: FastAPI
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    build_db(engine, tmp_path / "nope")
    _override_db(test_app, engine)
    try:
        response = await client.get("/library")
    finally:
        _clear_override(test_app)
    assert response.status_code == 200
    assert "0 rated items" in response.text
    assert "Nothing here yet." in response.text


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Seven Samurai", "seven samurai"),
        ("  Le Samouraï ", "le samourai"),
        ("Amélie", "amelie"),
        ("Ame\u0301lie", "amelie"),  # decomposed é folds like the composed form
        ("8½", "8½"),
        ("Léon: The Professional", "leon the professional"),
        ("Птица", "птица"),
        ("アダンの風", "アダンの風"),
        ("무너지기", "무너지기"),
        ("!!!", ""),
    ],
)
def test_normalize_title_folds_case_accents_and_punctuation(title: str, expected: str) -> None:
    assert normalize_title(title) == expected


def test_normalize_title_keeps_distinct_non_latin_titles_apart() -> None:
    # The old ASCII-only regex mapped every one of these to "", so title lookup
    # returned the first non-Latin item for all of them.
    titles = [
        "Птица",
        "Радио Африка",
        "シーナリィ",
        "무너지기",
        "Праздник на улице 36",
        "Уроборос: Улица 36",  # noqa: RUF001 — Cyrillic, deliberately confusable
    ]
    keys = [normalize_title(title) for title in titles]
    assert len(set(keys)) == len(titles)
