import logging
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from arthouse.enrich_music import (
    MusicEnrichment,
    album_descriptors,
    album_genres,
    artist_genres,
    enrich_music,
    enrich_music_item,
)
from arthouse.library import (
    Item,
    ItemGenre,
    ItemMeta,
    ItemMiss,
    ItemTag,
    build_db,
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
        self.search_calls: list[tuple[str, str]] = []

    def search_release_groups(
        self, title: str, artist: str = "", *, limit: int = 5
    ) -> list[ReleaseGroup]:
        del limit
        self.search_calls.append((title, artist))
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


def _batch_client(*, with_best: bool = True) -> FakeMusicBrainz:
    """Both fixture albums match; ratings put Best Album first."""
    hits = [_hit("mbid-1", "Fixture Album", "2018-04-01")]
    groups = {
        "mbid-1": _group(
            "mbid-1",
            "Fixture Album",
            "2018-04-01",
            genres=_genres(("art pop", 3)),
            tags=_tags(("melancholy", 5)),
        )
    }
    if with_best:
        hits.append(_hit("mbid-2", "Fixture Best Album", "2019-05-01", artist_id="artist-2"))
        groups["mbid-2"] = _group(
            "mbid-2",
            "Fixture Best Album",
            "2019-05-01",
            genres=_genres(("dream pop", 2), ("art pop", 1)),
            tags=_tags(("nocturnal", 3)),
        )
    return FakeMusicBrainz(hits, groups)


def test_batch_enriches_pending_albums_in_rating_order(library_db: Engine) -> None:
    client = _batch_client()

    stats = enrich_music(library_db, client)

    assert stats.matched == 2
    assert stats.already_enriched == 0
    assert stats.not_matched == 0
    assert stats.fallback == 0
    assert stats.genres == 3
    assert stats.descriptors == 2
    assert stats.unmatched == []
    # Highest rating first, matching the film pass.
    assert client.detail_calls == ["mbid-2", "mbid-1"]
    with Session(library_db) as db:
        assert db.get(ItemMeta, ("rateyourmusic", "2")) is not None
        assert db.get(ItemMeta, ("rateyourmusic", "1")) is not None


def _existing_meta(source_id: str, external_id: str) -> ItemMeta:
    """A row written by an earlier run, so the batch treats that album as done."""
    return ItemMeta(
        source="rateyourmusic",
        source_id=source_id,
        provider="musicbrainz",
        external_id=external_id,
        summary="",
        image_path=None,
        credit="Fixture Artist Two",
        tags_fetched=True,
        matched_title="Fixture Best Album",
        matched_year=2019,
    )


def test_batch_skips_already_enriched_albums(library_db: Engine) -> None:
    client = _batch_client()
    with Session(library_db) as db:
        db.add(_existing_meta("2", "mbid-2"))
        db.commit()

    stats = enrich_music(library_db, client)

    assert stats.matched == 1
    assert stats.already_enriched == 1
    assert stats.not_matched == 0
    assert client.detail_calls == ["mbid-1"]


def test_batch_limit_applies_to_pending_only(library_db: Engine) -> None:
    client = _batch_client()

    first = enrich_music(library_db, client, limit=1)
    assert first.matched == 1
    assert first.already_enriched == 0
    assert first.not_matched == 0
    assert client.detail_calls == ["mbid-2"]

    # The second run must move on instead of re-scanning the same album.
    second = enrich_music(library_db, client, limit=1)
    assert second.matched == 1
    assert second.already_enriched == 1
    assert second.not_matched == 0
    assert client.detail_calls == ["mbid-2", "mbid-1"]


def test_batch_reports_every_unmatched_album(library_db: Engine) -> None:
    stats = enrich_music(library_db, FakeMusicBrainz([]))

    assert stats.matched == 0
    assert stats.already_enriched == 0
    assert stats.not_matched == 0
    assert stats.unmatched == [
        "Fixture Best Album — Fixture Artist Two (2019)",
        "Fixture Album — Fixture Artist (2018)",
    ]
    with Session(library_db) as db:
        assert list(db.scalars(select(ItemMeta))) == []


def test_batch_counts_artist_fallback(library_db: Engine) -> None:
    client = FakeMusicBrainz(
        [_hit("mbid-1", "Fixture Album", "2018-04-01")],
        {"mbid-1": _group("mbid-1", "Fixture Album", "2018-04-01")},
        {"artist-1": {"genres": _genres(("art pop", 2), ("dream pop", 1))}},
    )
    with Session(library_db) as db:  # leave only the zero-genre album pending
        db.add(_existing_meta("2", "mbid-2"))
        db.commit()

    stats = enrich_music(library_db, client)

    assert stats.matched == 1
    assert stats.fallback == 1
    assert stats.genres == 2
    assert stats.descriptors == 0
    assert client.artist_calls == ["artist-1"]


def test_batch_logs_one_line_per_album(
    library_db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="arthouse.enrich_music")

    enrich_music(library_db, _batch_client())

    assert [record.getMessage() for record in caplog.records] == [
        "fetching [1/2] Fixture Best Album — Fixture Artist Two (2019)",
        "fetching [2/2] Fixture Album — Fixture Artist (2018)",
    ]


def test_batch_logs_a_recorded_miss_at_debug(
    library_db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="arthouse.enrich_music")

    enrich_music(library_db, FakeMusicBrainz([]))

    messages = [record.getMessage() for record in caplog.records]
    assert len([message for message in messages if message.endswith("recorded miss")]) == 2


def test_batch_logs_the_artist_fallback_at_debug(
    library_db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="arthouse.enrich_music")
    client = FakeMusicBrainz(
        [_hit("mbid-1", "Fixture Album", "2018-04-01")],
        {"mbid-1": _group("mbid-1", "Fixture Album", "2018-04-01")},
        {"artist-1": {"genres": _genres(("art pop", 2), ("dream pop", 1))}},
    )
    with Session(library_db) as db:  # leave only the zero-genre album pending
        db.add(_existing_meta("2", "mbid-2"))
        db.commit()

    enrich_music(library_db, client)

    assert "no album genres; artist fallback via artist-1" in [
        record.getMessage() for record in caplog.records
    ]


def test_unmatched_album_records_a_miss(library_db: Engine) -> None:
    with Session(library_db) as db:
        assert enrich_music_item(db, FakeMusicBrainz([]), _music_item(db)) is None

    with Session(library_db) as db:
        assert db.get(ItemMiss, ("rateyourmusic", "1", "musicbrainz")) is not None


def test_a_repeated_miss_keeps_one_row(library_db: Engine) -> None:
    client = FakeMusicBrainz([])

    with Session(library_db) as db:
        item = _music_item(db)
        assert enrich_music_item(db, client, item) is None
        assert enrich_music_item(db, client, item) is None

    with Session(library_db) as db:
        assert len(list(db.scalars(select(ItemMiss)))) == 1


def test_matched_album_records_no_miss(library_db: Engine) -> None:
    with Session(library_db) as db:
        assert enrich_music_item(db, _batch_client(), _music_item(db)) is not None

    with Session(library_db) as db:
        assert list(db.scalars(select(ItemMiss))) == []


def test_batch_does_not_retry_albums_that_missed(library_db: Engine) -> None:
    client = FakeMusicBrainz([])

    first = enrich_music(library_db, client)
    assert first.matched == 0
    assert first.already_enriched == 0
    assert first.not_matched == 0
    assert len(client.search_calls) == 2

    second = enrich_music(library_db, client)
    assert second.matched == 0
    assert second.already_enriched == 0
    assert second.not_matched == 2
    assert second.unmatched == []
    # No new requests: a recorded miss is a finished attempt.
    assert len(client.search_calls) == 2


def test_deleting_a_miss_queues_the_album_again(library_db: Engine) -> None:
    client = FakeMusicBrainz([])
    enrich_music(library_db, client)
    with Session(library_db) as db:
        miss = db.get(ItemMiss, ("rateyourmusic", "1", "musicbrainz"))
        assert miss is not None
        db.delete(miss)
        db.commit()

    enrich_music(library_db, client)

    assert client.search_calls[-1] == ("Fixture Album", "Fixture Artist")


def test_a_miss_survives_a_library_rebuild(library_db: Engine, tmp_path: Path) -> None:
    with Session(library_db) as db:
        assert enrich_music_item(db, FakeMusicBrainz([]), _music_item(db)) is None

    # `build_db` drops and recreates only the export-derived `items` table, so
    # a recorded attempt must outlive a restart.
    build_db(library_db, tmp_path)

    with Session(library_db) as db:
        assert db.get(ItemMiss, ("rateyourmusic", "1", "musicbrainz")) is not None
