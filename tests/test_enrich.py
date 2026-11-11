from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from arthouse.enrich import TmdbMovie, enrich_films, find_film, match_film
from arthouse.library import FilmGenre, FilmMeta, build_db


class FakeTmdb:
    """Network-free stand-in for TmdbClient."""

    def __init__(self, results: dict[str, list[TmdbMovie]]) -> None:
        self._results: dict[str, list[TmdbMovie]] = results

    def search_movie(self, title: str, year: int | None) -> list[TmdbMovie]:
        del year  # the fake is keyed by title only
        return self._results.get(title, [])

    def genre_map(self) -> dict[int, str]:
        return {18: "Drama", 878: "Science Fiction"}


def _movie(
    tmdb_id: int, title: str, release: str, votes: int, genres: list[int], overview: str = ""
) -> TmdbMovie:
    return {
        "id": tmdb_id,
        "title": title,
        "release_date": release,
        "vote_count": votes,
        "overview": overview,
        "poster_path": None,
        "genre_ids": genres,
    }


def test_match_prefers_the_year_compatible_candidate() -> None:
    results = [
        _movie(1, "Parasite", "1999-01-01", 999, [18]),
        _movie(2, "Parasite", "2019-05-30", 100, [18]),
    ]
    match = match_film(results, "Parasite", 2019)
    assert match is not None
    assert match["id"] == 2


def test_match_tolerates_one_year_drift() -> None:
    results = [_movie(3, "Casablanca", "1943-01-15", 500, [18])]
    match = match_film(results, "Casablanca", 1942)  # library year vs TMDB primary release
    assert match is not None
    assert match["id"] == 3


def test_match_ignores_fuzzy_titles() -> None:
    results = [_movie(4, "Parasite 2", "2019-01-01", 500, [18])]
    assert match_film(results, "Parasite", 2019) is None


def test_match_accepts_the_original_title() -> None:
    # TMDB lists Le Bonheur (1965) under its English title "Happiness".
    results: list[TmdbMovie] = [
        {
            "id": 53023,
            "title": "Happiness",
            "original_title": "Le Bonheur",
            "release_date": "1965-02-10",
            "vote_count": 323,
        }
    ]
    match = match_film(results, "Le Bonheur", 1965)
    assert match is not None
    assert match["id"] == 53023


def test_find_film_retries_with_a_shorter_query() -> None:
    fake = FakeTmdb(
        {
            "Jeanne Dielman": [
                _movie(
                    44012,
                    "Jeanne Dielman, 23, quai du Commerce, 1080 Bruxelles",
                    "1976-01-21",
                    422,
                    [18],
                )
            ]
        }
    )
    match = find_film(fake, "Jeanne Dielman, 23, quai du Commerce, 1080 Bruxelles", 1975)
    assert match is not None
    assert match["id"] == 44012


def test_enrichment_stores_meta_and_genres(library_db: Engine) -> None:
    fake = FakeTmdb(
        {
            "Fixture Film A": [
                _movie(1, "Fixture Film A", "1999-05-01", 100, [18, 878], "An A film.")
            ],
            "Fixture Film B": [_movie(2, "Fixture Film B", "2001-05-01", 50, [18], "A B film.")],
        }
    )
    stats = enrich_films(library_db, fake)
    assert stats.matched == 2
    assert stats.unmatched == []

    with Session(library_db) as db:
        metas = {m.source_id: m for m in db.scalars(select(FilmMeta))}
        genres = {(g.source_id, g.tag) for g in db.scalars(select(FilmGenre))}
    assert metas["https://boxd.it/aaa"].overview == "An A film."
    assert metas["https://boxd.it/aaa"].tmdb_id == 1
    assert genres == {
        ("https://boxd.it/aaa", "Drama"),
        ("https://boxd.it/aaa", "Science Fiction"),
        ("https://boxd.it/bbb", "Drama"),
    }


def test_enrichment_is_idempotent(library_db: Engine) -> None:
    fake = FakeTmdb({"Fixture Film A": [_movie(1, "Fixture Film A", "1999-05-01", 100, [18])]})
    first = enrich_films(library_db, fake)
    second = enrich_films(library_db, fake)
    assert first.matched == 1
    assert first.unmatched == ["Fixture Film B (2001)"]  # fake has no result for B
    assert second.matched == 0
    # Only enriched films are skipped; an unmatched film is retried next run
    # (a later TMDB entry may make it matchable) and reported again.
    assert second.skipped == 1
    assert second.unmatched == ["Fixture Film B (2001)"]


def test_enrichment_survives_library_rebuild(library_db: Engine, fixtures_dir: Path) -> None:
    fake = FakeTmdb(
        {"Fixture Film A": [_movie(1, "Fixture Film A", "1999-05-01", 100, [18], "Kept.")]}
    )
    enrich_films(library_db, fake)
    build_db(library_db, fixtures_dir)  # startup rebuild drops/recreates items only
    with Session(library_db) as db:
        meta = db.get(FilmMeta, ("letterboxd", "https://boxd.it/aaa"))
        assert meta is not None
        assert meta.overview == "Kept."
