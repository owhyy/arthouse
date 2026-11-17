"""Unified library from platform exports: parse CSVs, store in SQLite."""

# SQLAlchemy declarative columns are initialized by the ORM's instrumentation,
# which pyright cannot see — the rule is false-positives on this module only.
# pyright: reportUninitializedInstanceVariable=false

import csv
import html
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import (
    Engine,
    Index,
    Select,
    and_,
    create_engine,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import ConnectionPoolEntry

from arthouse.vocab import FACETS, Facet, facet_of

MEDIA_ORDER = ("music", "film", "book")
MEDIA_LABELS = {"music": "Music", "film": "Films", "book": "Books"}
PROVIDER_LABELS = {"tmdb": "TMDB", "musicbrainz": "MusicBrainz", "openlibrary": "OpenLibrary"}

# Display genres carry the provider's vote tally so the UI can rank them. The
# primary cut is a ratio of the best-voted genre rather than a fixed number,
# because vote totals differ by an order of magnitude between albums; below two
# votes there is no signal, so every genre stays primary and nothing is hidden.
PRIMARY_GENRE_RATIO = 0.4
PRIMARY_GENRE_MIN_COUNT = 2
PRIMARY_GENRE_MAX = 4


@dataclass(frozen=True)
class GenreRow:
    """One display genre and the provider's vote tally for it."""

    name: str
    count: int


class Base(DeclarativeBase):
    pass


def _fold_latin_accent(char: str) -> str:
    if char.isascii():
        return char
    base = "".join(
        ch for ch in unicodedata.normalize("NFKD", char) if not unicodedata.combining(ch)
    )
    return base if base.isascii() else char


def normalize_title(title: str) -> str:
    composed = unicodedata.normalize("NFC", title)
    folded = "".join(_fold_latin_accent(char) for char in composed).casefold()
    return re.sub(r"[^\w ]+", "", folded).strip()


_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_review(text: str) -> str | None:
    """Normalize review HTML (Goodreads) into plain text with paragraphs."""
    if not text.strip():
        return None
    text = _BREAK.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip() or None


def score_str(rating: float) -> str:
    """Display a 0-5 rating as a 0-10 score.

    x2 maps all three platforms losslessly: RYM 1-10 stays integer,
    Letterboxd half-stars become integers, Goodreads quarter steps become
    halves — so one decimal is always exact and never false precision.
    """
    return f"{rating * 2:g}"


class Item(Base):
    """One rated item from any platform."""

    __tablename__: str = "items"

    __table_args__: tuple[Index, ...] = (
        Index("ix_items_identity", "source", "source_id"),
        Index(
            "ix_items_media_rating_title",
            "media_type",
            text("rating DESC"),
            text("title COLLATE NOCASE"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_type: Mapped[str]  # music | film | book
    source: Mapped[str]  # letterboxd | rateyourmusic | goodreads
    source_id: Mapped[str]
    title: Mapped[str]
    creator: Mapped[str]
    year: Mapped[int | None]
    rating: Mapped[float]  # normalized to 0-5
    rating_native: Mapped[str]
    rated_on: Mapped[date | None]
    review: Mapped[str | None]

    @property
    def score(self) -> str:
        return score_str(self.rating)


class ItemMeta(Base):
    """Provider enrichment for one item; keyed by platform id so it survives rebuilds."""

    __tablename__: str = "item_meta"
    __table_args__: tuple[Index, ...] = (Index("ix_item_meta_provider", "provider"),)

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str]  # tmdb | musicbrainz | openlibrary
    external_id: Mapped[str]  # TMDB id, MusicBrainz MBID, OpenLibrary work id
    summary: Mapped[str]  # overview / description
    image_path: Mapped[str | None]  # poster / cover
    # None means "not fetched yet"; "" means the provider lists no credit.
    credit: Mapped[str | None] = mapped_column(default=None)
    # False means the provider's separate tag fetch has not been attempted yet;
    # an empty tag set is a valid result and must not be refetched every run.
    tags_fetched: Mapped[bool] = mapped_column(default=False)
    matched_title: Mapped[str]
    matched_year: Mapped[int | None]
    enriched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)


class ItemMiss(Base):
    """A provider lookup that found no match.

    Matching is strict, so a miss is a real answer and not something a rerun
    would fix. Recording it keeps the item out of the pending queue instead of
    spending requests on it every run; delete the row to try it again.
    When we implement other API sources for our metadata, these items should be refetched.
    """

    __tablename__: str = "item_miss"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class ItemGenre(Base):
    """One display genre for an item.

    Genres are kept apart from descriptor tags because they are shelf data,
    not vocabulary input. `count` is the provider's vote tally, which is what
    ranks the genres and decides the primary/secondary split; providers without
    votes (TMDB) leave it at zero. `artist_level` marks the fallback: the work
    itself had no genres, so the artist's were used and the UI may warn that
    they are approximate.
    """

    __tablename__: str = "item_genre"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    genre: Mapped[str] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    artist_level: Mapped[bool] = mapped_column(default=False)


class ItemTag(Base):
    """One raw descriptor label from an enrichment provider.

    Genres live in `item_genre`; this table holds only labels that the closed
    vocabulary may map (TMDB keywords, MusicBrainz tags, OpenLibrary subjects).
    The provider's own word for them is deliberately not stored. Raw tags stay
    the source of truth for the derived descriptors.
    """

    __tablename__: str = "item_tag"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    tag: Mapped[str] = mapped_column(primary_key=True)


class ItemDescriptor(Base):
    """One descriptor from the closed vocabulary attached to an item.

    Only the item identity and the descriptor name are stored: the facet lives
    in `arthouse.vocab`, so there is no second copy to drift out of sync.
    """

    __tablename__: str = "item_descriptor"
    __table_args__: tuple[Index, ...] = (Index("ix_item_descriptor_name", "name"),)

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        # RYM headers contain stray spaces (", First Name"); keys are stripped.
        # Rows with more values than headers get them under a None key as a
        # list — dropped.
        return [
            {k.strip(): v.strip() for k, v in row.items() if k is not None and isinstance(v, str)}
            for row in csv.DictReader(f)
        ]


def _parse_letterboxd(path: Path) -> list[Item]:
    items: list[Item] = []
    for row in _read_csv(path):
        rating = (row.get("Rating") or "").strip()
        if not rating:
            continue
        items.append(
            Item(
                media_type="film",
                source="letterboxd",
                source_id=row["Letterboxd URI"],
                title=row["Name"],
                creator="",
                year=int(row["Year"]) if row["Year"].strip() else None,
                rating=float(rating),
                rating_native=rating,
                rated_on=date.fromisoformat(row["Date"]) if row["Date"].strip() else None,
                review=None,
            )
        )
    return items


def _parse_rym(path: Path) -> list[Item]:
    items: list[Item] = []
    for row in _read_csv(path):
        rating = (row.get("Rating") or "").strip()
        if rating in ("", "0"):
            continue
        # RYM rates 1-10; prefer the romanized artist name when present.
        first = row["First Name localized"] or row["First Name"]
        last = row["Last Name localized"] or row["Last Name"]
        creator = f"{first} {last}" if first and last else (last or first)
        release_year = row["Release_Date"].strip()[:4]
        items.append(
            Item(
                media_type="music",
                source="rateyourmusic",
                source_id=row["RYM Album"],
                title=row["Title"],
                creator=creator,
                year=int(release_year) if release_year.isdigit() else None,
                rating=int(rating) / 2,
                rating_native=f"{rating}/10",
                rated_on=None,
                review=clean_review(row.get("Review", "")),
            )
        )
    return items


def _parse_goodreads(path: Path) -> list[Item]:
    items: list[Item] = []
    for row in _read_csv(path):
        rating = (row.get("My Rating") or "").strip()
        if not rating or float(rating) == 0:
            continue
        # to-read shelf = not yet consumed; carries no taste signal.
        if (row.get("Exclusive Shelf") or "").strip() == "to-read":
            continue
        read = row["Date Read"].strip()
        added = row["Date Added"].strip()
        rated_on = read or added
        year = row["Original Publication Year"].strip() or row["Year Published"].strip()
        items.append(
            Item(
                media_type="book",
                source="goodreads",
                source_id=row["Book Id"],
                title=row["Title"],
                creator=row["Author"],
                year=int(float(year)) if year else None,
                rating=float(rating),
                rating_native=rating,
                rated_on=(date(*map(int, rated_on.split("/"))) if rated_on else None),
                review=clean_review(row.get("My Review", "")),
            )
        )
    return items


def _letterboxd_reviews(path: Path) -> dict[tuple[str, int | None], str]:
    """Review text keyed by (normalized title, year).

    Letterboxd's reviews export uses different short-link URIs than an older
    ratings export (the format changed), so the URI cannot join them.
    """
    if not path.is_file():
        return {}
    reviews: dict[tuple[str, int | None], str] = {}
    for row in _read_csv(path):
        review = clean_review(row.get("Review", ""))
        if review is None:
            continue
        year = row.get("Year", "").strip()
        key = (normalize_title(row["Name"]), int(year) if year.isdigit() else None)
        previous = reviews.get(key)
        # Several viewings can each carry a review; keep them all.
        reviews[key] = f"{previous}\n\n{review}" if previous else review
    return reviews


def drop_legacy_enrichment(engine: Engine) -> None:
    """One-time reset of pre-refactor enrichment tables.

    They are regenerable from the providers, so dropping is cheaper and simpler
    than a schema migration. The removed `film_*` tables are the signal that
    this is an old database; clearing `item_meta`/`item_tag` prevents duplicate
    keys when enrichment is re-run. Re-run `python -m arthouse.enrich_films` to refill
    them.
    """
    inspector = inspect(engine)
    legacy = ("film_meta", "film_genre", "film_keyword")
    if not any(inspector.has_table(table) for table in legacy):
        return
    with engine.begin() as conn:
        for table in legacy:
            if inspector.has_table(table):
                conn.execute(text(f"DROP TABLE {table}"))
        for table in ("item_meta", "item_tag", "item_genre", "item_miss"):
            if inspector.has_table(table):
                conn.execute(text(f"DELETE FROM {table}"))


def create_db_engine(database: Path, *, echo: bool = False) -> Engine:
    """SQLite engine tuned for one writer and concurrent readers.

    WAL lets the enrichment passes write while the app keeps serving reads,
    `busy_timeout` waits for a blocked writer instead of failing immediately,
    and the larger cache keeps the derived tables in memory. `synchronous=NORMAL`
    is the recommended pairing with WAL for a local app: safe against process
    crashes, though a power loss can cost the last transaction.
    """
    engine = create_engine(f"sqlite:///{database}", echo=echo)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(
        dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA cache_size=-16000")
        cursor.close()

    return engine


def ensure_indexes(engine: Engine) -> None:
    """Create missing indexes on tables `create_all` left untouched.

    `create_all` skips a table that already exists together with its indexes,
    so a DB created before an index was added would never get it. Index DDL is
    additive and idempotent, so completing it here needs no data rebuild.
    """
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(engine, checkfirst=True)


def build_db(engine: Engine, data_dir: Path) -> int:
    """(Re)build the schema and items from data_dir exports; returns item count.

    Missing export files are skipped, so an empty data dir yields an empty
    library (CI has no data/).
    """
    parsers = {
        "letterboxd/ratings.csv": _parse_letterboxd,
        "rateyourmusic": _parse_rym,
        "goodreads/goodreads_library_export.csv": _parse_goodreads,
    }
    items: list[Item] = []
    for name, parse in parsers.items():
        # The RYM export filename contains the username; match on directory.
        paths = [data_dir / name] if "/" in name else sorted(data_dir.glob(f"{name}/*.csv"))
        for path in paths:
            if path.is_file():
                items.extend(parse(path))

    # Letterboxd keeps review text in a separate export file.
    reviews = _letterboxd_reviews(data_dir / "letterboxd/reviews.csv")
    for item in items:
        if item.source == "letterboxd" and item.review is None:
            item.review = reviews.get((normalize_title(item.title), item.year))

    # Only the export-derived items table is rebuilt; enrichment tables
    # (item_meta, item_tag) persist across restarts.
    Base.metadata.tables["items"].drop(engine, checkfirst=True)
    Base.metadata.create_all(engine)
    ensure_indexes(engine)
    drop_legacy_enrichment(engine)
    with Session(engine) as session:
        session.add_all(items)
        session.commit()
    return len(items)


def _items_query(
    media_type: str | None = None,
    search: str | None = None,
    descriptor: str | None = None,
) -> Select[Item]:
    stmt = select(Item)
    if search:
        stmt = stmt.where(Item.title.ilike(f"%{search}%"))
    if descriptor:
        stmt = stmt.join(
            ItemDescriptor,
            and_(
                ItemDescriptor.source == Item.source,
                ItemDescriptor.source_id == Item.source_id,
            ),
        ).where(ItemDescriptor.name == descriptor)
    if media_type is not None:
        stmt = stmt.where(Item.media_type == media_type)
    return stmt


def query_items(
    db: Session,
    media_type: str | None = None,
    search: str | None = None,
    descriptor: str | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[Item]:
    """Rated items, newest rating first; `search` matches the title only.

    The grid passes `limit`/`offset` so SQLite sorts and slices the page;
    `count_items` supplies the total the pager shows.
    """
    stmt = _items_query(media_type, search, descriptor).order_by(
        Item.rating.desc(), Item.title.collate("nocase")
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def count_items(
    db: Session,
    media_type: str | None = None,
    search: str | None = None,
    descriptor: str | None = None,
) -> int:
    """Number of items the grid would match, without loading the rows."""
    count = db.scalar(
        select(func.count()).select_from(_items_query(media_type, search, descriptor).subquery())
    )
    return count or 0


def count_items_by_media(db: Session) -> dict[str, int]:
    """Rated items per medium for the tab strip, in one aggregate query."""
    rows = db.execute(select(Item.media_type, func.count()).group_by(Item.media_type)).all()
    return {media: count for media, count in rows}


@dataclass(frozen=True)
class DescriptorOption:
    name: str
    facet: Facet
    count: int


def query_descriptor_options(
    db: Session, media_type: str, per_facet: int = 30
) -> list[tuple[Facet, list[DescriptorOption]]]:
    """Descriptors used in one medium, grouped by facet.

    Only descriptors that actually occur are listed, so a small vocabulary
    needs no `min_count` cut-off the way the 2,800-term keyword list did.
    """
    rows = db.execute(
        select(ItemDescriptor.name, func.count())
        .join(
            Item,
            and_(
                Item.source == ItemDescriptor.source,
                Item.source_id == ItemDescriptor.source_id,
            ),
        )
        .where(Item.media_type == media_type)
        .group_by(ItemDescriptor.name)
    ).all()
    groups: dict[Facet, list[DescriptorOption]] = {}
    for name, count in rows:
        facet = facet_of(name)
        if facet is None:  # a stored name the vocabulary no longer knows
            continue
        groups.setdefault(facet, []).append(DescriptorOption(name, facet, count))
    for options in groups.values():
        options.sort(key=lambda option: (-option.count, option.name))
    return [(facet, groups[facet][:per_facet]) for facet in FACETS if facet in groups]


def query_item_descriptor_groups(
    db: Session, source: str, source_id: str
) -> list[tuple[Facet, list[str]]]:
    """Descriptors for one item, grouped by facet in vocabulary order."""
    names = db.scalars(
        select(ItemDescriptor.name).where(
            ItemDescriptor.source == source,
            ItemDescriptor.source_id == source_id,
        )
    ).all()
    groups: dict[Facet, list[str]] = {}
    for name in names:
        facet = facet_of(name)
        if facet is None:
            continue
        groups.setdefault(facet, []).append(name)
    return [(facet, sorted(groups[facet])) for facet in FACETS if facet in groups]


def query_item(db: Session, item_id: int) -> Item | None:
    return db.get(Item, item_id)


def query_item_meta(
    db: Session, source: str, source_id: str
) -> tuple[ItemMeta, list[GenreRow]] | None:
    """Provider metadata and display genres for one item, or None when unenriched."""
    meta = db.get(ItemMeta, (source, source_id))
    if meta is None:
        return None
    rows = db.execute(
        select(ItemGenre.genre, ItemGenre.count)
        .where(
            ItemGenre.source == source,
            ItemGenre.source_id == source_id,
        )
        .order_by(ItemGenre.count.desc(), ItemGenre.genre)
    ).all()
    return meta, [GenreRow(name, count) for name, count in rows]


def query_item_credits(db: Session, media_type: str) -> dict[str, str]:
    """Provider credits (director, …) keyed by platform id for one grid tab."""
    rows = db.execute(
        select(ItemMeta.source_id, ItemMeta.credit)
        .join(
            Item,
            and_(Item.source == ItemMeta.source, Item.source_id == ItemMeta.source_id),
        )
        .where(Item.media_type == media_type)
    ).all()
    return {source_id: credit for source_id, credit in rows if credit}


def query_item_genres(
    db: Session,
    media_type: str,
    source_ids: Sequence[str] | None = None,
) -> dict[str, list[GenreRow]]:
    """Display genres keyed by platform id, best-voted first.

    Passing the platform ids of the visible page keeps this query proportional
    to the page instead of the whole medium. `None` loads every id for the CLIs
    and reports that still need the full picture.
    """
    if source_ids is not None and not source_ids:
        return {}
    stmt = (
        select(ItemGenre.source_id, ItemGenre.genre, ItemGenre.count)
        .join(
            Item,
            and_(Item.source == ItemGenre.source, Item.source_id == ItemGenre.source_id),
        )
        .where(Item.media_type == media_type)
        .order_by(ItemGenre.count.desc(), ItemGenre.genre)
    )
    if source_ids is not None:
        stmt = stmt.where(ItemGenre.source_id.in_(source_ids))
    genres: dict[str, list[GenreRow]] = {}
    for source_id, genre, count in db.execute(stmt).all():
        genres.setdefault(source_id, []).append(GenreRow(genre, count))
    return genres


def split_genres(genres: Sequence[GenreRow]) -> tuple[list[GenreRow], list[GenreRow]]:
    """Split ranked genres into primary and secondary for display.

    Input must be in rank order. A genre is primary when its tally is at least
    `PRIMARY_GENRE_RATIO` of the top genre's and it has at least
    `PRIMARY_GENRE_MIN_COUNT` votes, capped at `PRIMARY_GENRE_MAX` so a cluster
    of equally-voted sub-genres cannot take over the header. Providers without
    votes leave every count at zero, which falls through to "all primary" and
    keeps their display unchanged.
    """
    if not genres:
        return [], []
    top = genres[0].count
    if top < PRIMARY_GENRE_MIN_COUNT:
        return list(genres), []
    cut = max(PRIMARY_GENRE_MIN_COUNT, math.ceil(top * PRIMARY_GENRE_RATIO))
    # Ranked input makes the qualifying genres a prefix, so the cap can slice.
    primary = [genre for genre in genres if genre.count >= cut][:PRIMARY_GENRE_MAX]
    return primary, list(genres[len(primary) :])
