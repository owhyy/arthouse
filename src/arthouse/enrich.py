"""TMDB film enrichment: match films, store description and genre tags.

Run with: uv run python -m arthouse.enrich [--limit N]

Enrichment is keyed by (source, source_id) so it survives library rebuilds;
re-runs skip films that are already enriched.
"""

import argparse
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import NotRequired, Protocol, TypedDict, cast

import httpx
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from arthouse.config import get_settings
from arthouse.library import FilmGenre, FilmMeta, Item, normalize_title

TMDB_BASE_URL = "https://api.themoviedb.org/3"
YEAR_TOLERANCE = 1  # Letterboxd/library years can differ from TMDB's primary release
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 2.0  # seconds; used when TMDB sends no Retry-After header


class TmdbMovie(TypedDict):
    id: int
    title: str
    vote_count: int
    original_title: NotRequired[str]
    release_date: NotRequired[str]
    overview: NotRequired[str]
    poster_path: NotRequired[str | None]
    genre_ids: NotRequired[list[int]]


class TmdbSearchResponse(TypedDict, total=False):
    results: list[TmdbMovie]


class TmdbGenre(TypedDict):
    id: int
    name: str


class TmdbGenreResponse(TypedDict, total=False):
    genres: list[TmdbGenre]


def _titles(movie: TmdbMovie) -> tuple[str, ...]:
    """TMDB's `title` is localized (Le Bonheur is listed as "Happiness")."""
    return (movie.get("title", ""), movie.get("original_title", ""))


def match_film(results: list[TmdbMovie], title: str, year: int | None) -> TmdbMovie | None:
    """Pick the best candidate: exact normalized title, year within tolerance.

    Returns None when nothing matches exactly — a fuzzy guess is worse than a
    reported miss, because wrong genres poison the recommender.
    """
    wanted = normalize_title(title)
    exact = [m for m in results if wanted in {normalize_title(t) for t in _titles(m)}]
    if year is not None:
        close = [
            m
            for m in exact
            if (m.get("release_date") or "")[:4].isdigit()
            and abs(int((m.get("release_date") or "")[:4]) - year) <= YEAR_TOLERANCE
        ]
        pool = close or exact
    else:
        pool = exact
    if not pool:
        return None
    return max(pool, key=lambda m: m.get("vote_count", 0))


def search_queries(title: str) -> list[str]:
    """Full title first, then the part before the first comma.

    TMDB's search returns nothing for very long titles such as
    "Jeanne Dielman, 23, quai du Commerce, 1080 Bruxelles".
    """
    queries = [title]
    short = title.split(",")[0].strip()
    if short and short != title:
        queries.append(short)
    return queries


def find_film(client: TmdbLike, title: str, year: int | None) -> TmdbMovie | None:
    """Search, then match exactly; try shorter queries before giving up."""
    for query in search_queries(title):
        if match := match_film(client.search_movie(query, year), title, year):
            return match
    return None


class TmdbLike(Protocol):
    def search_movie(self, title: str, year: int | None) -> list[TmdbMovie]: ...

    def genre_map(self) -> dict[int, str]: ...


class TmdbClient:
    """Thin TMDB v3 client; accepts a v3 API key or a v4 read access token."""

    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        if api_key.startswith("eyJ"):  # v4 read access token
            headers["Authorization"] = f"Bearer {api_key}"
        else:  # v3 API key
            params["api_key"] = api_key
        self._client: httpx.Client = httpx.Client(
            base_url=TMDB_BASE_URL, headers=headers, params=params, timeout=timeout
        )
        self._genres: dict[int, str] | None = None

    def __enter__(self) -> TmdbClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, str | int] | None = None) -> object:
        """GET with retries, so one 429/5xx does not abort a 300-film batch."""
        for attempt in range(MAX_ATTEMPTS):
            response = self._client.get(path, params=params)
            transient = response.status_code == 429 or response.status_code >= 500
            if transient and attempt < MAX_ATTEMPTS - 1:
                # httpx types Headers.get as Any (it forwards an untyped default).
                retry_after = cast(str | None, response.headers.get("Retry-After"))
                try:
                    wait = float(retry_after) if retry_after is not None else RETRY_BACKOFF
                except ValueError:
                    wait = RETRY_BACKOFF
                time.sleep(wait)
                continue
            response.raise_for_status()
            return cast(object, response.json())
        raise AssertionError("unreachable: the last attempt either returns or raises")

    def search_movie(self, title: str, year: int | None) -> list[TmdbMovie]:
        params: dict[str, str | int] = {"query": title}
        if year is not None:
            params["year"] = year
        payload = cast(TmdbSearchResponse, self._get("/search/movie", params))
        return payload.get("results", [])

    def genre_map(self) -> dict[int, str]:
        if self._genres is None:
            payload = cast(TmdbGenreResponse, self._get("/genre/movie/list"))
            self._genres = {g["id"]: g["name"] for g in payload.get("genres", [])}
        return self._genres


@dataclass
class EnrichStats:
    matched: int = 0
    skipped: int = 0
    unmatched: list[str] = field(default_factory=list)


def enrich_films(engine: Engine, client: TmdbLike, limit: int | None = None) -> EnrichStats:
    """Enrich up to `limit` not-yet-enriched films; commits after each match."""
    stats = EnrichStats()
    genre_names = client.genre_map()
    with Session(engine) as db:
        done = {(meta.source, meta.source_id) for meta in db.scalars(select(FilmMeta))}
        films = list(
            db.scalars(
                select(Item)
                .where(Item.media_type == "film")
                .order_by(Item.rating.desc(), Item.title)
            )
        )
        # Skip before applying the limit, otherwise a second `--limit 5` run
        # would re-scan the same five already-enriched films and do nothing.
        pending = [f for f in films if (f.source, f.source_id) not in done]
        stats.skipped = len(films) - len(pending)
        if limit is not None:
            pending = pending[:limit]
        for film in pending:
            match = find_film(client, film.title, film.year)
            if match is None:
                stats.unmatched.append(f"{film.title} ({film.year})")
                continue
            release = match.get("release_date") or ""
            db.add(
                FilmMeta(
                    source=film.source,
                    source_id=film.source_id,
                    tmdb_id=match["id"],
                    overview=match.get("overview", ""),
                    poster_path=match.get("poster_path"),
                    matched_title=match.get("title", ""),
                    matched_year=int(release[:4]) if release[:4].isdigit() else None,
                )
            )
            for genre_id in match.get("genre_ids", []):
                if name := genre_names.get(genre_id):
                    db.add(FilmGenre(source=film.source, source_id=film.source_id, tag=name))
            db.commit()
            stats.matched += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich films with TMDB description and genres.")
    parser.add_argument("--limit", type=int, default=None, help="process at most N films")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.tmdb_api_key:
        raise SystemExit("ARTHOUSE_TMDB_API_KEY is not set (see README)")
    engine = create_engine(f"sqlite:///{settings.data_dir / 'library.db'}")
    with TmdbClient(settings.tmdb_api_key) as client:
        stats = enrich_films(engine, client, limit=cast(int | None, args.limit))
    print(f"matched: {stats.matched}, skipped: {stats.skipped}, unmatched: {len(stats.unmatched)}")
    for line in stats.unmatched:
        print(f"  unmatched: {line}")


if __name__ == "__main__":
    main()
