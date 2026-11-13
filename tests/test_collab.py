import csv
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from arthouse.collab import (
    CollabMatrix,
    LibraryFilm,
    build_matrix,
    library_films,
    load_collab,
    load_links,
    neighbors,
)
from arthouse.library import ItemMeta, query_items


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _add_tmdb_ids(engine: Engine, tmdb_ids: list[int]) -> list[str]:
    with Session(engine) as db:
        films = query_items(db, media_type="film")
        for item, tmdb_id in zip(films, tmdb_ids, strict=True):
            db.add(
                ItemMeta(
                    source=item.source,
                    source_id=item.source_id,
                    provider="tmdb",
                    external_id=str(tmdb_id),
                    summary="",
                    image_path=None,
                    credit="",
                    matched_title=item.title,
                    matched_year=item.year,
                )
            )
        db.commit()
        return [item.title for item in films]


def test_load_links_skips_rows_without_tmdb_id(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "links.csv",
        ["movieId", "imdbId", "tmdbId"],
        [["101", "0114709", "9001"], ["102", "0113497", ""]],
    )
    assert load_links(path) == {9001: 101}


def test_library_films_join_through_tmdb_id(library_db: Engine, tmp_path: Path) -> None:
    titles = _add_tmdb_ids(library_db, [9001, 9002])
    links = _write_csv(
        tmp_path / "links.csv",
        ["movieId", "imdbId", "tmdbId"],
        [["101", "x", "9001"], ["102", "x", "9002"], ["103", "x", "9003"]],
    )
    films = library_films(library_db, load_links(links))
    assert sorted(films) == [101, 102]
    assert {film.title for film in films.values()} == set(titles)


def test_load_collab_keeps_only_library_ratings(library_db: Engine, tmp_path: Path) -> None:
    _add_tmdb_ids(library_db, [9001, 9002])
    links = _write_csv(
        tmp_path / "links.csv",
        ["movieId", "imdbId", "tmdbId"],
        [["101", "x", "9001"], ["102", "x", "9002"]],
    )
    ratings = _write_csv(
        tmp_path / "ratings.csv",
        ["userId", "movieId", "rating", "timestamp"],
        [
            ["1", "101", "5.0", "0"],
            ["1", "102", "4.0", "0"],
            ["1", "999", "1.0", "0"],
            ["2", "101", "4.0", "0"],
            ["2", "102", "5.0", "0"],
        ],
    )
    matrix = load_collab(library_db, ratings, links)
    assert set(matrix.vectors) == {101, 102}
    assert matrix.users == 2
    assert matrix.ratings == 5  # every row is read, only library films are kept


def _matrix() -> CollabMatrix:
    films = {
        1: LibraryFilm(1, "letterboxd", "a", "Alpha", 2000),
        2: LibraryFilm(2, "letterboxd", "b", "Beta", 2001),
        3: LibraryFilm(3, "letterboxd", "c", "Gamma", 2002),
        4: LibraryFilm(4, "letterboxd", "d", "Delta", 2003),
    }
    vectors = {
        1: {10: 1.0, 11: 0.5, 12: -0.5},
        2: {10: 0.8, 11: 0.4, 12: -0.4},
        3: {10: -1.0},
        4: {10: 0.9},
    }
    return build_matrix(films, vectors, users=3, ratings=8)


def test_neighbors_rank_by_adjusted_cosine() -> None:
    results = neighbors(_matrix(), "alpha", k=10, min_overlap=2)
    assert [result.film.title for result in results] == ["Beta"]
    assert results[0].score == pytest.approx(1.0)
    assert results[0].overlap == 3


def test_neighbors_drop_negative_similarity() -> None:
    # Gamma is anti-correlated with Alpha, so it never shows up.
    assert "Gamma" not in [result.film.title for result in neighbors(_matrix(), 1, min_overlap=1)]


def test_min_overlap_filters_thin_pairs() -> None:
    assert [result.film.title for result in neighbors(_matrix(), 1, min_overlap=2)] == ["Beta"]
    assert [result.film.title for result in neighbors(_matrix(), 1, min_overlap=1)] == [
        "Beta",
        "Delta",
    ]


def test_neighbors_unknown_title_is_empty() -> None:
    assert neighbors(_matrix(), "not a film") == []
