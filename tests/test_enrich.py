from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from arthouse.enrich import TmdbMovie, enrich_films, find_film, keyword_report, match_film
from arthouse.library import ItemMeta, ItemTag, build_db


class FakeTmdb:
    """Network-free stand-in for TmdbClient."""

    def __init__(
        self,
        results: dict[str, list[TmdbMovie]],
        directors: dict[int, str] | None = None,
        keywords: dict[int, list[str]] | None = None,
    ) -> None:
        self._results: dict[str, list[TmdbMovie]] = results
        self._directors: dict[int, str] = directors or {}
        self._keywords: dict[int, list[str]] = keywords or {}
        self.keyword_calls: int = 0

    def search_movie(self, title: str, year: int | None) -> list[TmdbMovie]:
        del year  # the fake is keyed by title only
        return self._results.get(title, [])

    def genre_map(self) -> dict[int, str]:
        return {18: "Drama", 878: "Science Fiction"}

    def directors(self, tmdb_id: int) -> str:
        return self._directors.get(tmdb_id, f"Director {tmdb_id}")

    def keywords(self, tmdb_id: int) -> list[str]:
        self.keyword_calls += 1
        return self._keywords.get(tmdb_id, [])


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


def test_match_rejects_out_of_tolerance_years() -> None:
    # TMDB's `year` filter is loose (a 2019 search can return the 1999 film of
    # the same name), so the year check must happen here. No in-tolerance
    # candidate means a reported miss, not the remake.
    results = [_movie(5, "Parasite", "1999-01-01", 500, [18])]
    assert match_film(results, "Parasite", 2019) is None


def test_match_rejects_undated_candidates_when_a_year_is_known() -> None:
    results = [_movie(6, "Parasite", "", 500, [18])]
    assert match_film(results, "Parasite", 2019) is None


def test_match_keeps_exact_title_for_yearless_items() -> None:
    results = [_movie(7, "Parasite", "1999-01-01", 500, [18])]
    assert match_film(results, "Parasite", None) is not None


def test_match_ignores_fuzzy_titles() -> None:
    results = [_movie(4, "Parasite 2", "2019-01-01", 500, [18])]
    assert match_film(results, "Parasite", 2019) is None


def test_match_rejects_symbol_only_titles() -> None:
    # Such a title normalizes to "", which must not be treated as "equal" to
    # every other symbol-only title.
    results = [_movie(8, "!!!", "2019-01-01", 500, [18])]
    assert match_film(results, "???", 2019) is None


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
        },
        keywords={1: ["dystopia", "nihilism"], 2: ["drama"]},
    )
    stats = enrich_films(library_db, fake)
    assert stats.matched == 2
    assert stats.unmatched == []

    with Session(library_db) as db:
        metas = {m.source_id: m for m in db.scalars(select(ItemMeta))}
        genres = {
            (tag.source_id, tag.tag)
            for tag in db.scalars(select(ItemTag).where(ItemTag.origin == "genre"))
        }
        keywords = {
            (tag.source_id, tag.tag)
            for tag in db.scalars(select(ItemTag).where(ItemTag.origin == "keyword"))
        }
    assert metas["https://boxd.it/aaa"].summary == "An A film."
    assert metas["https://boxd.it/aaa"].external_id == "1"
    assert metas["https://boxd.it/aaa"].credit == "Director 1"
    assert metas["https://boxd.it/aaa"].tags_fetched
    assert genres == {
        ("https://boxd.it/aaa", "Drama"),
        ("https://boxd.it/aaa", "Science Fiction"),
        ("https://boxd.it/bbb", "Drama"),
    }
    assert keywords == {
        ("https://boxd.it/aaa", "dystopia"),
        ("https://boxd.it/aaa", "nihilism"),
        ("https://boxd.it/bbb", "drama"),
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
    assert fake.keyword_calls == 1  # already-fetched keywords are not refetched


def test_directors_are_backfilled_for_older_rows(library_db: Engine) -> None:
    with Session(library_db) as db:
        db.add(
            ItemMeta(
                source="letterboxd",
                source_id="https://boxd.it/aaa",
                provider="tmdb",
                external_id="42",
                summary="Old row.",
                image_path=None,
                matched_title="Fixture Film A",
                matched_year=1999,
            )
        )
        db.commit()
    fake = FakeTmdb({}, directors={42: "Akira Kurosawa"})
    stats = enrich_films(library_db, fake)
    assert stats.backfilled == 1
    assert stats.keywords == 1  # older rows get keywords too
    assert stats.matched == 0  # B is unmatched, A is already enriched
    with Session(library_db) as db:
        meta = db.get(ItemMeta, ("letterboxd", "https://boxd.it/aaa"))
    assert meta is not None
    assert meta.credit == "Akira Kurosawa"


def test_keywords_are_backfilled_for_older_rows(library_db: Engine) -> None:
    with Session(library_db) as db:
        db.add(
            ItemMeta(
                source="letterboxd",
                source_id="https://boxd.it/aaa",
                provider="tmdb",
                external_id="42",
                summary="Old row.",
                image_path=None,
                matched_title="Fixture Film A",
                matched_year=1999,
            )
        )
        db.commit()
    fake = FakeTmdb({}, keywords={42: ["dystopia", "nihilism"]})
    stats = enrich_films(library_db, fake)
    assert stats.keywords == 1
    with Session(library_db) as db:
        rows = {
            (tag.source_id, tag.tag)
            for tag in db.scalars(select(ItemTag).where(ItemTag.origin == "keyword"))
        }
        meta = db.get(ItemMeta, ("letterboxd", "https://boxd.it/aaa"))
    assert rows == {
        ("https://boxd.it/aaa", "dystopia"),
        ("https://boxd.it/aaa", "nihilism"),
    }
    assert meta is not None
    assert meta.tags_fetched


def test_empty_keyword_list_is_not_refetched(library_db: Engine) -> None:
    fake = FakeTmdb(
        {"Fixture Film A": [_movie(1, "Fixture Film A", "1999-05-01", 100, [18])]},
        keywords={1: []},
    )
    enrich_films(library_db, fake)
    assert fake.keyword_calls == 1
    enrich_films(library_db, fake)
    assert fake.keyword_calls == 1  # an empty result still counts as fetched


def test_keyword_report_counts_coverage(library_db: Engine) -> None:
    fake = FakeTmdb(
        {
            "Fixture Film A": [_movie(1, "Fixture Film A", "1999-05-01", 100, [18])],
            "Fixture Film B": [_movie(2, "Fixture Film B", "2001-05-01", 50, [18])],
        },
        keywords={1: ["dystopia", "nihilism"], 2: []},
    )
    enrich_films(library_db, fake)
    report = keyword_report(library_db)
    assert report.films == 2
    assert report.fetched == 2
    assert report.empty == 1
    assert report.rows == 2
    assert report.distinct == 2
    assert report.top == [("dystopia", 1), ("nihilism", 1)]


def test_enrichment_survives_library_rebuild(library_db: Engine, fixtures_dir: Path) -> None:
    fake = FakeTmdb(
        {"Fixture Film A": [_movie(1, "Fixture Film A", "1999-05-01", 100, [18], "Kept.")]}
    )
    enrich_films(library_db, fake)
    build_db(library_db, fixtures_dir)  # startup rebuild drops/recreates items only
    with Session(library_db) as db:
        meta = db.get(ItemMeta, ("letterboxd", "https://boxd.it/aaa"))
        assert meta is not None
        assert meta.summary == "Kept."
