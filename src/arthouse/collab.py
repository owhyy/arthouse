"""MovieLens collaborative-filtering baseline for the film library.

Run with: uv run python -m arthouse.collab [--title TITLE]... [--ratings PATH]

The MovieLens exports are local-only (`datasets/` is git-ignored). Similarities
are computed only between films already in the library, so the output is
directly comparable with the content kNN in `arthouse.descriptors`.
"""

import argparse
import csv
import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from sqlalchemy import Engine, and_, create_engine, select
from sqlalchemy.orm import Session

from arthouse.config import BASE_DIR, get_settings
from arthouse.descriptors import load_matrix
from arthouse.descriptors import neighbors as content_neighbors
from arthouse.library import Item, ItemMeta, normalize_title

DEFAULT_RATINGS = BASE_DIR / "datasets" / "ratings_small.csv"
DEFAULT_LINKS = BASE_DIR / "datasets" / "links.csv"


@dataclass(frozen=True)
class LibraryFilm:
    movie_id: int
    source: str
    source_id: str
    title: str
    year: int | None


def load_links(path: Path) -> dict[int, int]:
    """TMDB id -> MovieLens movieId; rows without a TMDB id are skipped."""
    mapping: dict[int, int] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            tmdb_id = row["tmdbId"].strip()
            if tmdb_id:
                mapping[int(tmdb_id)] = int(row["movieId"])
    return mapping


def library_films(engine: Engine, links: dict[int, int]) -> dict[int, LibraryFilm]:
    """Library films MovieLens knows about, keyed by movieId.

    The join goes through `item_meta.external_id` (the TMDB id), so a film
    needs TMDB enrichment before it can take part in collaborative filtering.
    """
    films: dict[int, LibraryFilm] = {}
    with Session(engine) as db:
        rows = db.execute(
            select(Item.source, Item.source_id, Item.title, Item.year, ItemMeta.external_id)
            .join(
                ItemMeta,
                and_(
                    ItemMeta.source == Item.source,
                    ItemMeta.source_id == Item.source_id,
                ),
            )
            .where(Item.media_type == "film", ItemMeta.provider == "tmdb")
        ).all()
    for source, source_id, title, year, external_id in rows:
        try:
            tmdb_id = int(external_id)
        except ValueError:  # a non-numeric external id cannot be a TMDB id
            continue
        movie_id = links.get(tmdb_id)
        if movie_id is None or movie_id in films:
            continue
        films[movie_id] = LibraryFilm(movie_id, source, source_id, title, year)
    return films


def _rating_rows(path: Path) -> Iterator[tuple[int, int, float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            yield int(row[0]), int(row[1]), float(row[2])


@dataclass
class CollabMatrix:
    """Mean-centred item vectors over MovieLens users, L2-normalized on demand."""

    films: dict[int, LibraryFilm]
    vectors: dict[int, dict[int, float]]
    norms: dict[int, float]
    users: int
    ratings: int
    by_title: dict[str, list[int]] = field(default_factory=dict)


def build_matrix(
    films: dict[int, LibraryFilm],
    vectors: dict[int, dict[int, float]],
    users: int,
    ratings: int,
) -> CollabMatrix:
    """Assemble a matrix from centred vectors, dropping all-zero rows."""
    kept: dict[int, dict[int, float]] = {}
    norms: dict[int, float] = {}
    for movie_id, vector in vectors.items():
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm > 0:
            kept[movie_id] = vector
            norms[movie_id] = norm
    by_title: defaultdict[str, list[int]] = defaultdict(list)
    for movie_id, film in films.items():
        if movie_id in kept:
            by_title[normalize_title(film.title)].append(movie_id)
    return CollabMatrix(
        films=films,
        vectors=kept,
        norms=norms,
        users=users,
        ratings=ratings,
        by_title=dict(by_title),
    )


def load_collab(
    engine: Engine,
    ratings_path: Path = DEFAULT_RATINGS,
    links_path: Path = DEFAULT_LINKS,
) -> CollabMatrix:
    """Two passes over the ratings file: user means, then library vectors.

    Only ratings for library films are kept in memory, so the same code runs on
    `ratings_small.csv` and on the 26M-row `ratings.csv`.
    """
    films = library_films(engine, load_links(links_path))

    sums: defaultdict[int, float] = defaultdict(float)
    counts: defaultdict[int, int] = defaultdict(int)
    total = 0
    for user_id, _movie_id, rating in _rating_rows(ratings_path):
        sums[user_id] += rating
        counts[user_id] += 1
        total += 1

    vectors: defaultdict[int, dict[int, float]] = defaultdict(dict)
    for user_id, movie_id, rating in _rating_rows(ratings_path):
        if movie_id in films:
            vectors[movie_id][user_id] = rating - sums[user_id] / counts[user_id]

    return build_matrix(films, dict(vectors), users=len(counts), ratings=total)


def _resolve(matrix: CollabMatrix, key: str | int) -> int | None:
    if isinstance(key, int):
        return key if key in matrix.vectors else None
    matches = matrix.by_title.get(normalize_title(key))
    return matches[0] if matches else None


def find_film(matrix: CollabMatrix, key: str | int) -> LibraryFilm | None:
    """Resolve a title (first match) or a MovieLens movieId."""
    movie_id = _resolve(matrix, key)
    return matrix.films[movie_id] if movie_id is not None else None


@dataclass
class CollabNeighbor:
    film: LibraryFilm
    score: float
    overlap: int


def neighbors(
    matrix: CollabMatrix,
    key: str | int,
    k: int = 10,
    min_overlap: int = 5,
) -> list[CollabNeighbor]:
    """Adjusted-cosine neighbours inside the library; [] when unknown or isolated."""
    movie_id = _resolve(matrix, key)
    if movie_id is None:
        return []
    target = matrix.vectors[movie_id]
    target_norm = matrix.norms[movie_id]
    scored: list[CollabNeighbor] = []
    for other_id, other in matrix.vectors.items():
        if other_id == movie_id:
            continue
        small, large = (target, other) if len(target) <= len(other) else (other, target)
        dot = 0.0
        overlap = 0
        for user_id, value in small.items():
            other_value = large.get(user_id)
            if other_value is not None:
                dot += value * other_value
                overlap += 1
        if overlap < min_overlap or dot <= 0:
            continue
        score = dot / (target_norm * matrix.norms[other_id])
        scored.append(CollabNeighbor(matrix.films[other_id], score, overlap))
    scored.sort(key=lambda neighbor: (-neighbor.score, neighbor.film.title.casefold()))
    return scored[:k]


def _label(title: str, year: int | None) -> str:
    return f"{title} ({year})" if year else title


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MovieLens collaborative-filtering baseline (library-internal neighbours)."
    )
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="film title to find neighbours for (repeatable)",
    )
    parser.add_argument("--ratings", type=Path, default=DEFAULT_RATINGS, help="ratings CSV")
    parser.add_argument("--links", type=Path, default=DEFAULT_LINKS, help="links CSV")
    parser.add_argument("--k", type=int, default=10, help="neighbours to print")
    parser.add_argument(
        "--min-overlap",
        type=int,
        default=5,
        help="minimum users who rated both films",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="also print content kNN neighbours for the same titles",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.data_dir / 'library.db'}")
    matrix = load_collab(engine, cast(Path, args.ratings), cast(Path, args.links))
    print(
        f"collab: {matrix.users} users, {matrix.ratings} ratings, "
        + f"{len(matrix.vectors)}/{len(matrix.films)} library films rated "
        + f"(min_overlap={cast(int, args.min_overlap)})"
    )

    content = load_matrix(engine) if cast(bool, args.compare) else None
    for title in cast(list[str], args.title):
        film = find_film(matrix, title)
        if film is None:
            print(f"\n{title}: not in the collaborative matrix")
            continue
        print(f"\n{_label(film.title, film.year)} — {len(matrix.vectors[film.movie_id])} ratings")
        results = neighbors(
            matrix,
            film.movie_id,
            k=cast(int, args.k),
            min_overlap=cast(int, args.min_overlap),
        )
        if not results:
            print("  no collaborative neighbours")
        for result in results:
            print(
                f"  {result.score:.3f}  {_label(result.film.title, result.film.year)}"
                + f"  ({result.overlap} co-raters)"
            )
        if content is not None:
            print("  content kNN:")
            matches = content_neighbors(content, (film.source, film.source_id), k=cast(int, args.k))
            if not matches:
                print("    no shared descriptors")
            for match in matches:
                print(f"    {match.score:.3f}  {_label(match.item.title, match.item.year)}")


if __name__ == "__main__":
    main()
