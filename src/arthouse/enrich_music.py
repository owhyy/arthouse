"""MusicBrainz music enrichment: turn a matched album into library rows.

Album-level genres are display data; album-level community tags are descriptor
input. Genres may fall back to the artist and are stored with `artist_level`,
because some albums have no curated genres of their own. Artist tags are never
used: a tag describes one album's mood, and the same artist can be
introspective on one record and playful on the next.

This module owns one album at a time and the batch loop that drives it; films
have their own module (`enrich_films`).
"""

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from arthouse.config import get_settings
from arthouse.db import create_db_engine, create_schema
from arthouse.logs import configure_logging
from arthouse.models import GenreRow, Item, ItemGenre, ItemMeta, ItemMiss, ItemTag
from arthouse.musicbrainz import (
    Artist,
    Genre,
    MusicBrainzClient,
    MusicClient,
    ReleaseGroup,
    Tag,
    find_release_group,
    release_year,
)
from arthouse.tagging import rebuild_descriptors

MUSIC_PROVIDER = "musicbrainz"

log = logging.getLogger("arthouse.enrich_music")


def _positive_names(entries: Sequence[Tag | Genre]) -> list[str]:
    """Names the community has voted up, deduped, in payload order."""
    return list(dict.fromkeys(entry["name"] for entry in entries if entry["count"] > 0))


def _positive_genres(entries: Sequence[Tag | Genre]) -> list[GenreRow]:
    """Voted-up genres with their tallies, deduped by name, best vote kept."""
    best: dict[str, int] = {}
    for entry in entries:
        if entry["count"] > 0:
            best[entry["name"]] = max(best.get(entry["name"], 0), entry["count"])
    return [GenreRow(name, count) for name, count in best.items()]


def album_genres(group: ReleaseGroup) -> list[GenreRow]:
    """Curated genres for the album itself, with their tallies; display-only."""
    return _positive_genres(group.get("genres", []))


def album_descriptors(group: ReleaseGroup) -> list[str]:
    """Community tags for the album itself; the descriptor source."""
    return _positive_names(group.get("tags", []))


def artist_genres(artist: Artist) -> list[GenreRow]:
    """Curated genres from the artist fallback, with their tallies; display-only."""
    return _positive_genres(artist.get("genres", []))


def _add_genres(
    db: Session, item: Item, genres: Sequence[GenreRow], *, artist_level: bool = False
) -> None:
    """Add display genres; rows are already deduped and vote-filtered."""
    for genre in genres:
        db.add(
            ItemGenre(
                source=item.source,
                source_id=item.source_id,
                provider=MUSIC_PROVIDER,
                genre=genre.name,
                count=genre.count,
                artist_level=artist_level,
            )
        )


def _add_tags(db: Session, item: Item, names: list[str]) -> None:
    """Add raw descriptor labels; names are already deduped and vote-filtered."""
    for name in names:
        db.add(
            ItemTag(
                source=item.source,
                source_id=item.source_id,
                provider=MUSIC_PROVIDER,
                tag=name,
            )
        )


def _artist_credit(group: ReleaseGroup) -> str:
    """The credited names, which are not always the canonical artist names."""
    return ", ".join(credit["name"] for credit in group.get("artist-credit", []))


def _artist_id(group: ReleaseGroup) -> str | None:
    """The first credited artist's MusicBrainz id, for the genre fallback."""
    credits = group.get("artist-credit") or []
    if not credits:
        return None
    artist = credits[0].get("artist")
    return artist["id"] if artist is not None else None


@dataclass(frozen=True)
class MusicEnrichment:
    """What one matched album wrote, for the batch report."""

    external_id: str
    matched_title: str
    matched_year: int | None
    album_genres: int
    album_descriptors: int
    artist_genres: int
    artist_fallback: bool


def enrich_music_item(db: Session, client: MusicClient, item: Item) -> MusicEnrichment | None:
    """Write one album's metadata, genres and descriptors; None when unmatched.

    The detail lookup is always made, even when the search hit already carries
    tags, because only the detail endpoint has curated genres. The hit supplies
    the artist credit and the fallback id: the detail payload does not request
    `artist-credit`.
    """
    hit = find_release_group(client, item.title, item.creator, item.year)
    if hit is None:
        # Remember the miss: the title is matched exactly, so a rerun would
        # search the same query and miss again. Delete the row to retry.
        if db.get(ItemMiss, (item.source, item.source_id, MUSIC_PROVIDER)) is None:
            db.add(
                ItemMiss(
                    source=item.source,
                    source_id=item.source_id,
                    provider=MUSIC_PROVIDER,
                )
            )
            db.commit()
            log.debug("no match for %s; recorded miss", _label(item))
        else:
            log.debug("no match for %s; miss already recorded", _label(item))
        return None

    group = client.release_group(hit["id"])
    genres = album_genres(group)
    descriptors = album_descriptors(group)
    log.debug(
        "matched %s %r: %d album genres, %d tags",
        hit["id"],
        group["title"],
        len(genres),
        len(descriptors),
    )
    fallback_genres: list[GenreRow] = []
    artist_fallback = not genres
    if artist_fallback and (artist_id := _artist_id(hit)) is not None:
        log.debug("no album genres; artist fallback via %s", artist_id)
        fallback_genres = artist_genres(client.artist(artist_id))

    matched_year = release_year(group)
    if matched_year is None:
        matched_year = release_year(hit)
    db.add(
        ItemMeta(
            source=item.source,
            source_id=item.source_id,
            provider=MUSIC_PROVIDER,
            external_id=group["id"],
            summary="",
            image_path=None,
            credit=_artist_credit(hit),
            tags_fetched=True,
            matched_title=group["title"],
            matched_year=matched_year,
        )
    )
    _add_genres(db, item, genres)
    _add_tags(db, item, descriptors)
    _add_genres(db, item, fallback_genres, artist_level=True)
    db.commit()
    return MusicEnrichment(
        external_id=group["id"],
        matched_title=group["title"],
        matched_year=matched_year,
        album_genres=len(genres),
        album_descriptors=len(descriptors),
        artist_genres=len(fallback_genres),
        artist_fallback=artist_fallback,
    )


@dataclass
class MusicStats:
    """What one batch did, for the CLI summary."""

    matched: int = 0
    already_enriched: int = 0  # metadata from an earlier run
    not_matched: int = 0  # a strict no-match recorded by an earlier run
    fallback: int = 0
    genres: int = 0
    descriptors: int = 0
    unmatched: list[str] = field(default_factory=list)


def _label(item: Item) -> str:
    """One-line identification for the unmatched report; title alone collides."""
    label = f"{item.title} — {item.creator}" if item.creator else item.title
    return f"{label} ({item.year})" if item.year is not None else label


def enrich_music(engine: Engine, client: MusicClient, limit: int | None = None) -> MusicStats:
    """Enrich up to `limit` not-yet-enriched albums; each match commits itself.

    Albums whose previous lookup missed are skipped too: the matcher is strict,
    so a miss would only be repeated and would keep taking the first `--limit`
    slots. Delete the `item_miss` row to have the album tried again.
    """
    stats = MusicStats()
    with Session(engine) as db:
        done = {
            (meta.source, meta.source_id)
            for meta in db.scalars(select(ItemMeta).where(ItemMeta.provider == MUSIC_PROVIDER))
        }
        missed = {
            (miss.source, miss.source_id)
            for miss in db.scalars(select(ItemMiss).where(ItemMiss.provider == MUSIC_PROVIDER))
        }
        # A miss is a finished attempt too; otherwise the same top-rated
        # unmatched albums are retried, and take the first slots, every run.
        attempted = done | missed
        albums = list(
            db.scalars(
                select(Item)
                .where(Item.media_type == "music")
                .order_by(Item.rating.desc(), Item.title)
            )
        )
        # Skip before applying the limit, otherwise a second `--limit 5` run
        # would re-scan the same five already-enriched albums and do nothing.
        pending = [album for album in albums if (album.source, album.source_id) not in attempted]
        # Report the two skip reasons apart: "skipped" alone hid whether an
        # album was done or had been given up on.
        album_keys = {(album.source, album.source_id) for album in albums}
        stats.already_enriched = len(album_keys & done)
        stats.not_matched = len(album_keys & missed)
        if limit is not None:
            pending = pending[:limit]

        for index, album in enumerate(pending, start=1):
            # One line per album keeps a ten-minute run watchable at INFO;
            # everything else (queries, retries, payload detail) is DEBUG.
            log.info("fetching [%d/%d] %s", index, len(pending), _label(album))
            result = enrich_music_item(db, client, album)
            if result is None:
                stats.unmatched.append(_label(album))
            else:
                stats.matched += 1
                stats.fallback += result.artist_fallback
                stats.genres += result.album_genres + result.artist_genres
                stats.descriptors += result.album_descriptors
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich music albums with MusicBrainz metadata, genres, and tags."
    )
    parser.add_argument("--limit", type=int, default=None, help="process at most N albums")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    engine = create_db_engine(settings.data_dir / "library.db")
    # The app creates the schema at startup, but the CLI may run first.
    create_schema(engine)
    with MusicBrainzClient(settings.musicbrainz_user_agent) as client:
        stats = enrich_music(engine, client, limit=cast(int | None, args.limit))
    rebuild_descriptors(engine)  # keep the derived vocabulary in sync
    print(
        f"matched: {stats.matched}, already enriched: {stats.already_enriched}, "
        + f"not matched in musicbrainz: {stats.not_matched}, "
        + f"artist fallback: {stats.fallback}, genres: {stats.genres}, "
        + f"descriptors: {stats.descriptors}, unmatched: {len(stats.unmatched)}"
    )
    for line in stats.unmatched:
        print(f"  unmatched: {line}")


if __name__ == "__main__":
    main()
