"""MusicBrainz music enrichment: turn a matched album into library rows.

Album-level genres are display data; album-level community tags are descriptor
input. Both come from the album itself only: an album MusicBrainz has no genre
for is left without genres.

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
from arthouse.enrichment import item_label, record_miss
from arthouse.logs import configure_logging
from arthouse.models import GenreRow, Item, ItemGenre, ItemMeta, ItemMiss, ItemTag
from arthouse.musicbrainz import (
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


@dataclass(frozen=True)
class TagRow:
    """One raw descriptor label and the provider's vote tally for it."""

    name: str
    count: int


def _voted_counts(entries: Sequence[Tag | Genre]) -> dict[str, int]:
    """Positive vote tallies by label, deduped keeping the best count."""
    best: dict[str, int] = {}
    for entry in entries:
        if entry["count"] > 0:
            best[entry["name"]] = max(best.get(entry["name"], 0), entry["count"])
    return best


def _positive_tags(entries: Sequence[Tag | Genre]) -> list[TagRow]:
    """Voted-up tag labels with their tallies, deduped by name, best vote kept."""
    return [TagRow(name, count) for name, count in _voted_counts(entries).items()]


def _positive_genres(entries: Sequence[Tag | Genre]) -> list[GenreRow]:
    """Voted-up genres with their tallies, deduped by name, best vote kept."""
    return [GenreRow(name, count) for name, count in _voted_counts(entries).items()]


def album_genres(group: ReleaseGroup) -> list[GenreRow]:
    """Curated genres for the album itself, with their tallies; display-only."""
    return _positive_genres(group.get("genres", []))


def album_descriptors(group: ReleaseGroup) -> list[TagRow]:
    """Community tags for the album itself, with their tallies; descriptor input."""
    return _positive_tags(group.get("tags", []))


def _add_genres(db: Session, item: Item, genres: Sequence[GenreRow]) -> None:
    """Add display genres; rows are already deduped and vote-filtered."""
    for genre in genres:
        db.add(
            ItemGenre(
                source=item.source,
                source_id=item.source_id,
                provider=MUSIC_PROVIDER,
                genre=genre.name,
                count=genre.count,
            )
        )


def _add_tags(db: Session, item: Item, tags: Sequence[TagRow]) -> None:
    """Add raw descriptor labels; tags are already deduped and vote-filtered."""
    for tag in tags:
        db.add(
            ItemTag(
                source=item.source,
                source_id=item.source_id,
                provider=MUSIC_PROVIDER,
                tag=tag.name,
                count=tag.count,
            )
        )


def _artist_credit(group: ReleaseGroup) -> str:
    """The credited names, which are not always the canonical artist names."""
    return ", ".join(credit["name"] for credit in group.get("artist-credit", []))


@dataclass(frozen=True)
class MusicEnrichment:
    """What one matched album wrote, for the batch report."""

    external_id: str
    matched_title: str
    matched_year: int | None
    album_genres: int
    album_descriptors: int


def enrich_music_item(db: Session, client: MusicClient, item: Item) -> MusicEnrichment | None:
    """Write one album's metadata, genres and descriptors; None when unmatched.

    The detail lookup is always made, even when the search hit already carries
    tags, because only the detail endpoint has curated genres.
    """
    hit = find_release_group(client, item.title, item.creator, item.year)
    if hit is None:
        # Remember the miss: the title is matched exactly, so a rerun would
        # search the same query and miss again. Delete the row to retry.
        if record_miss(db, item, MUSIC_PROVIDER):
            log.debug("no match for %s; recorded miss", item_label(item))
        else:
            log.debug("no match for %s; miss already recorded", item_label(item))
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
            credit=_artist_credit(hit),
            tags_fetched=True,
            matched_title=group["title"],
            matched_year=matched_year,
        )
    )
    _add_genres(db, item, genres)
    _add_tags(db, item, descriptors)
    db.commit()
    return MusicEnrichment(
        external_id=group["id"],
        matched_title=group["title"],
        matched_year=matched_year,
        album_genres=len(genres),
        album_descriptors=len(descriptors),
    )


@dataclass
class MusicStats:
    """What one batch did, for the CLI summary."""

    matched: int = 0
    already_enriched: int = 0  # metadata from an earlier run
    not_matched: int = 0  # a strict no-match recorded by an earlier run
    genres: int = 0
    descriptors: int = 0
    unmatched: list[str] = field(default_factory=list)


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
            log.info("fetching [%d/%d] %s", index, len(pending), item_label(album))
            result = enrich_music_item(db, client, album)
            if result is None:
                stats.unmatched.append(item_label(album))
            else:
                stats.matched += 1
                stats.genres += result.album_genres
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
        + f"genres: {stats.genres}, descriptors: {stats.descriptors}, "
        + f"unmatched: {len(stats.unmatched)}"
    )
    for line in stats.unmatched:
        print(f"  unmatched: {line}")


if __name__ == "__main__":
    main()
