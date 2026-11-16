"""MusicBrainz music enrichment: turn a matched album into library rows.

Album-level genres are display data; album-level community tags are descriptor
input. Genres may fall back to the artist and are stored with `artist_level`,
because some albums have no curated genres of their own. Artist tags are never
used: a tag describes one album's mood, and the same artist can be
introspective on one record and playful on the next.

This module owns one album at a time; the batch loop and its CLI land later.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from arthouse.library import Item, ItemGenre, ItemMeta, ItemTag
from arthouse.musicbrainz import (
    Artist,
    Genre,
    MusicClient,
    ReleaseGroup,
    Tag,
    find_release_group,
    release_year,
)

MUSIC_PROVIDER = "musicbrainz"


def _positive_names(entries: Sequence[Tag | Genre]) -> list[str]:
    """Names the community has voted up, deduped, in payload order."""
    return list(dict.fromkeys(entry["name"] for entry in entries if entry["count"] > 0))


def album_genres(group: ReleaseGroup) -> list[str]:
    """Curated genres for the album itself; display-only."""
    return _positive_names(group.get("genres", []))


def album_descriptors(group: ReleaseGroup) -> list[str]:
    """Community tags for the album itself; the descriptor source."""
    return _positive_names(group.get("tags", []))


def artist_genres(artist: Artist) -> list[str]:
    """Curated genres from the artist fallback; display-only."""
    return _positive_names(artist.get("genres", []))


def _add_genres(db: Session, item: Item, names: list[str], *, artist_level: bool = False) -> None:
    """Add display genres; names are already deduped and vote-filtered."""
    for name in names:
        db.add(
            ItemGenre(
                source=item.source,
                source_id=item.source_id,
                provider=MUSIC_PROVIDER,
                genre=name,
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
        return None

    group = client.release_group(hit["id"])
    genres = album_genres(group)
    descriptors = album_descriptors(group)
    fallback_genres: list[str] = []
    artist_fallback = not genres
    if artist_fallback and (artist_id := _artist_id(hit)) is not None:
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
