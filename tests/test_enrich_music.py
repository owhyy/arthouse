from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from arthouse.enrich_music import (
    MusicEnrichment,
    album_descriptors,
    album_genres,
    artist_genres,
    enrich_music_item,
)
from arthouse.library import (
    Item,
    ItemGenre,
    ItemMeta,
    ItemTag,
    query_item_genres,
    query_item_meta,
    query_items,
)
from arthouse.musicbrainz import Artist, Genre, ReleaseGroup, Tag


class FakeMusicBrainz:
    """Network-free client with one search result and canned detail payloads."""

    def __init__(
        self,
        hits: list[ReleaseGroup],
        groups: dict[str, ReleaseGroup] | None = None,
        artists: dict[str, Artist] | None = None,
    ) -> None:
        self.hits: list[ReleaseGroup] = hits
        self.groups: dict[str, ReleaseGroup] = groups or {}
        self.artists: dict[str, Artist] = artists or {}
        self.detail_calls: list[str] = []
        self.artist_calls: list[str] = []

    def search_release_groups(
        self, title: str, artist: str = "", *, limit: int = 5
    ) -> list[ReleaseGroup]:
        del title, artist, limit
        return self.hits

    def release_group(self, mbid: str) -> ReleaseGroup:
        self.detail_calls.append(mbid)
        return self.groups[mbid]

    def artist(self, mbid: str) -> Artist:
        self.artist_calls.append(mbid)
        return self.artists[mbid]


def _genres(*pairs: tuple[str, int]) -> list[Genre]:
    return [{"name": name, "count": count} for name, count in pairs]


def _tags(*pairs: tuple[str, int]) -> list[Tag]:
    return [{"name": name, "count": count} for name, count in pairs]


def _hit(
    mbid: str,
    title: str,
    date: str,
    *,
    artist_id: str = "artist-1",
    credit: str = "Fixture Artist",
) -> ReleaseGroup:
    return {
        "id": mbid,
        "title": title,
        "primary-type": "Album",
        "first-release-date": date,
        "artist-credit": [{"name": credit, "artist": {"id": artist_id}}],
    }


def _group(
    mbid: str,
    title: str,
    date: str,
    *,
    genres: list[Genre] | None = None,
    tags: list[Tag] | None = None,
) -> ReleaseGroup:
    return {
        "id": mbid,
        "title": title,
        "primary-type": "Album",
        "first-release-date": date,
        "genres": genres or [],
        "tags": tags or [],
    }


def _music_item(db: Session, title: str = "Fixture Album") -> Item:
    return next(item for item in query_items(db, media_type="music") if item.title == title)


def test_album_mapping_keeps_genres_and_descriptors_separate() -> None:
    group = _group(
        "mbid-1",
        "Fixture Album",
        "2018",
        genres=_genres(("art pop", 3), ("rejected genre", 0)),
        tags=_tags(("melancholy", 5), ("melancholy", 2), ("rejected tag", -1)),
    )
    assert album_genres(group) == ["art pop"]
    assert album_descriptors(group) == ["melancholy"]


def test_artist_mapping_reads_genres_only() -> None:
    artist: Artist = {
        "genres": _genres(("art pop", 4), ("rejected genre", 0)),
    }
    assert artist_genres(artist) == ["art pop"]


def test_matched_album_writes_meta_and_album_rows(library_db: Engine) -> None:
    hit = _hit("mbid-1", "Fixture Album", "2018-04-01")
    group = _group(
        "mbid-1",
        "Fixture Album",
        "2018-04-01",
        genres=_genres(("art pop", 3), ("rejected genre", 0)),
        tags=_tags(("melancholy", 5), ("rejected tag", 0), ("melancholy", 1)),
    )
    client = FakeMusicBrainz([hit], {"mbid-1": group})

    with Session(library_db) as db:
        result = enrich_music_item(db, client, _music_item(db))

    assert result == MusicEnrichment(
        external_id="mbid-1",
        matched_title="Fixture Album",
        matched_year=2018,
        album_genres=1,
        album_descriptors=1,
        artist_genres=0,
        artist_fallback=False,
    )
    assert client.detail_calls == ["mbid-1"]
    assert client.artist_calls == []
    with Session(library_db) as db:
        meta = db.get(ItemMeta, ("rateyourmusic", "1"))
        genres = {(row.genre, row.artist_level) for row in db.scalars(select(ItemGenre))}
        tags = {row.tag for row in db.scalars(select(ItemTag))}
    assert meta is not None
    assert meta.provider == "musicbrainz"
    assert meta.external_id == "mbid-1"
    assert meta.summary == ""
    assert meta.image_path is None
    assert meta.credit == "Fixture Artist"
    assert meta.tags_fetched
    assert meta.matched_title == "Fixture Album"
    assert meta.matched_year == 2018
    assert genres == {("art pop", False)}
    assert tags == {"melancholy"}


def test_artist_genre_fallback_fires_when_the_album_has_no_genres(
    library_db: Engine,
) -> None:
    hit = _hit("mbid-2", "Fixture Album", "2018-04-01", artist_id="artist-9")
    group = _group(
        "mbid-2",
        "Fixture Album",
        "2018-04-01",
        genres=_genres(("unvoted", 0)),
        tags=_tags(("melancholy", 5)),
    )
    artist: Artist = {
        "name": "Fixture Artist",
        "genres": _genres(("art pop", 4), ("dream pop", 1)),
    }
    client = FakeMusicBrainz([hit], {"mbid-2": group}, {"artist-9": artist})

    with Session(library_db) as db:
        result = enrich_music_item(db, client, _music_item(db))

    assert result is not None
    assert result.artist_fallback
    assert result.album_genres == 0
    assert result.album_descriptors == 1
    assert result.artist_genres == 2
    assert client.artist_calls == ["artist-9"]
    with Session(library_db) as db:
        genres = {(row.genre, row.artist_level) for row in db.scalars(select(ItemGenre))}
        tags = {row.tag for row in db.scalars(select(ItemTag))}
        found = query_item_meta(db, "rateyourmusic", "1")
        grid = query_item_genres(db, "music")
    assert found is not None
    meta, display_genres = found
    assert meta is not None
    assert genres == {("art pop", True), ("dream pop", True)}
    assert tags == {"melancholy"}
    assert display_genres == ["art pop", "dream pop"]
    assert grid == {"1": ["art pop", "dream pop"]}


def test_album_without_tags_does_not_borrow_artist_tags(library_db: Engine) -> None:
    """Artist tags describe a career, not one album, so they are never used."""
    hit = _hit("mbid-4", "Fixture Album", "2018-04-01", artist_id="artist-9")
    group = _group("mbid-4", "Fixture Album", "2018-04-01", genres=_genres(("art pop", 3)))
    client = FakeMusicBrainz([hit], {"mbid-4": group})

    with Session(library_db) as db:
        result = enrich_music_item(db, client, _music_item(db))

    assert result is not None
    assert not result.artist_fallback
    assert result.album_genres == 1
    assert result.album_descriptors == 0
    assert result.artist_genres == 0
    assert client.artist_calls == []
    with Session(library_db) as db:
        genres = {(row.genre, row.artist_level) for row in db.scalars(select(ItemGenre))}
        tags = list(db.scalars(select(ItemTag)))
    assert genres == {("art pop", False)}
    assert tags == []


def test_artist_fallback_is_skipped_without_a_credit(library_db: Engine) -> None:
    hit: ReleaseGroup = {
        "id": "mbid-3",
        "title": "Fixture Album",
        "primary-type": "Album",
        "first-release-date": "2018-04-01",
    }
    group = _group("mbid-3", "Fixture Album", "2018-04-01", tags=_tags(("unvoted", 0)))
    client = FakeMusicBrainz([hit], {"mbid-3": group})

    with Session(library_db) as db:
        result = enrich_music_item(db, client, _music_item(db))

    assert result is not None
    assert result.artist_fallback
    assert result.album_genres == 0
    assert result.album_descriptors == 0
    assert result.artist_genres == 0
    assert client.artist_calls == []
    with Session(library_db) as db:
        meta = db.get(ItemMeta, ("rateyourmusic", "1"))
        genres = list(db.scalars(select(ItemGenre)))
        tags = list(db.scalars(select(ItemTag)))
    assert meta is not None
    assert meta.credit == ""
    assert genres == []
    assert tags == []


def test_unmatched_album_writes_nothing(library_db: Engine) -> None:
    client = FakeMusicBrainz([])

    with Session(library_db) as db:
        assert enrich_music_item(db, client, _music_item(db)) is None

    assert client.detail_calls == []
    with Session(library_db) as db:
        assert list(db.scalars(select(ItemMeta))) == []
        assert list(db.scalars(select(ItemGenre))) == []
        assert list(db.scalars(select(ItemTag))) == []
