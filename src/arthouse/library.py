"""Unified library from platform exports: parse CSVs, store in SQLite."""

# SQLAlchemy declarative columns are initialized by the ORM's instrumentation,
# which pyright cannot see — the rule is false-positives on this module only.
# pyright: reportUninitializedInstanceVariable=false

import csv
import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import Engine, and_, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

MEDIA_ORDER = ("music", "film", "book")
MEDIA_LABELS = {"music": "Music", "film": "Films", "book": "Books"}
FACET_ORDER = ("theme", "mood", "style", "setting", "production")


class Base(DeclarativeBase):
    pass


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation — used to match titles across exports."""
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()


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


class FilmMeta(Base):
    """TMDB enrichment for a film; keyed by platform id so it survives rebuilds."""

    __tablename__: str = "film_meta"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int]
    overview: Mapped[str]
    poster_path: Mapped[str | None]
    # None means "not fetched yet"; "" means TMDB lists no director.
    director: Mapped[str | None] = mapped_column(default=None)
    # False means "keywords not fetched yet" — an empty keyword set is a
    # valid result and must not be refetched on every run.
    keywords_fetched: Mapped[bool] = mapped_column(default=False)
    matched_title: Mapped[str]
    matched_year: Mapped[int | None]
    enriched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class ItemTag(Base):
    """One raw tag from an enrichment provider.

    `origin` says what kind of tag it is (TMDB genre vs keyword, MusicBrainz
    release-group vs artist tag, OpenLibrary subject); `provider` says where it
    came from. Raw tags stay the source of truth for the derived descriptors.
    """

    __tablename__: str = "item_tag"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    origin: Mapped[str] = mapped_column(primary_key=True)
    tag: Mapped[str] = mapped_column(primary_key=True)


class Descriptor(Base):
    """Normalized descriptor vocabulary with a facet (theme, mood, …)."""

    __tablename__: str = "descriptor"

    media_type: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
    facet: Mapped[str]


class ItemDescriptor(Base):
    """One normalized descriptor attached to an item."""

    __tablename__: str = "item_descriptor"

    media_type: Mapped[str] = mapped_column(primary_key=True)
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
    """One-time reset of the film-only enrichment tables.

    They are regenerable from TMDB, so dropping is cheaper and simpler than a
    schema migration. `film_genre`/`film_keyword` no longer exist in the model,
    so their presence is the signal that this is a pre-refactor database.
    Re-run `python -m arthouse.enrich` to refill `film_meta` and `item_tag`.
    """
    inspector = inspect(engine)
    if not any(inspector.has_table(table) for table in ("film_genre", "film_keyword")):
        return
    with engine.begin() as conn:
        for table in ("film_meta", "film_genre", "film_keyword"):
            if inspector.has_table(table):
                conn.execute(text(f"DROP TABLE {table}"))


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
    # (film_meta, item_tag) persist across restarts.
    Base.metadata.tables["items"].drop(engine, checkfirst=True)
    Base.metadata.create_all(engine)
    drop_legacy_enrichment(engine)
    with Session(engine) as session:
        session.add_all(items)
        session.commit()
    return len(items)


def query_items(
    db: Session,
    media_type: str | None = None,
    search: str | None = None,
    descriptor: str | None = None,
) -> list[Item]:
    """Rated items, newest rating first; `search` matches the title only."""
    stmt = select(Item)
    if search:
        stmt = stmt.where(Item.title.ilike(f"%{search}%"))
    if descriptor:
        stmt = stmt.join(
            ItemDescriptor,
            and_(
                ItemDescriptor.media_type == Item.media_type,
                ItemDescriptor.source == Item.source,
                ItemDescriptor.source_id == Item.source_id,
            ),
        ).where(ItemDescriptor.name == descriptor)
    stmt = stmt.order_by(Item.rating.desc(), Item.title.collate("nocase"))
    if media_type is not None:
        stmt = stmt.where(Item.media_type == media_type)
    return list(db.scalars(stmt))


@dataclass(frozen=True)
class DescriptorOption:
    name: str
    facet: str
    count: int


def query_descriptor_options(
    db: Session, media_type: str = "film", min_count: int = 2, per_facet: int = 30
) -> list[tuple[str, list[DescriptorOption]]]:
    """Top descriptors per facet; rare ones stay reachable via detail chips."""
    rows = db.execute(
        select(Descriptor.name, Descriptor.facet, func.count(ItemDescriptor.name))
        .join(
            ItemDescriptor,
            and_(
                ItemDescriptor.media_type == Descriptor.media_type,
                ItemDescriptor.name == Descriptor.name,
            ),
        )
        .join(
            Item,
            and_(
                Item.source == ItemDescriptor.source,
                Item.source_id == ItemDescriptor.source_id,
            ),
        )
        .where(Item.media_type == media_type)
        .group_by(Descriptor.name, Descriptor.facet)
        .having(func.count(ItemDescriptor.name) >= min_count)
        .order_by(Descriptor.facet, func.count(ItemDescriptor.name).desc(), Descriptor.name)
    ).all()
    groups: dict[str, list[DescriptorOption]] = {}
    for name, facet, count in rows:
        groups.setdefault(facet, []).append(DescriptorOption(name, facet, count))
    return [(facet, groups[facet][:per_facet]) for facet in FACET_ORDER if facet in groups]


def query_item_descriptor_groups(
    db: Session, media_type: str, source: str, source_id: str
) -> list[tuple[str, list[str]]]:
    """Canonical descriptors for one item, grouped by facet and sorted."""
    rows = db.execute(
        select(Descriptor.facet, Descriptor.name)
        .join(
            ItemDescriptor,
            and_(
                ItemDescriptor.media_type == Descriptor.media_type,
                ItemDescriptor.name == Descriptor.name,
            ),
        )
        .where(
            ItemDescriptor.media_type == media_type,
            ItemDescriptor.source == source,
            ItemDescriptor.source_id == source_id,
        )
    ).all()
    groups: dict[str, list[str]] = {}
    for facet, name in rows:
        groups.setdefault(facet, []).append(name)
    return [(facet, sorted(groups[facet])) for facet in FACET_ORDER if facet in groups]


def query_item(db: Session, item_id: int) -> Item | None:
    return db.get(Item, item_id)


def query_film_meta(db: Session, source: str, source_id: str) -> tuple[FilmMeta, list[str]] | None:
    """TMDB metadata and genre tags for one film, or None when unenriched."""
    meta = db.get(FilmMeta, (source, source_id))
    if meta is None:
        return None
    genres = list(
        db.scalars(
            select(ItemTag.tag)
            .where(
                ItemTag.source == source,
                ItemTag.source_id == source_id,
                ItemTag.origin == "genre",
            )
            .order_by(ItemTag.tag)
        )
    )
    return meta, genres


def query_film_directors(db: Session) -> dict[str, str]:
    """Director names keyed by platform id — one query for the whole grid."""
    return {meta.source_id: meta.director for meta in db.scalars(select(FilmMeta)) if meta.director}


def query_item_genres(db: Session, media_type: str) -> dict[str, list[str]]:
    """Genre tags keyed by platform id — one query for the whole grid."""
    genres: dict[str, list[str]] = {}
    rows = db.execute(
        select(ItemTag.source_id, ItemTag.tag)
        .join(
            Item,
            and_(Item.source == ItemTag.source, Item.source_id == ItemTag.source_id),
        )
        .where(Item.media_type == media_type, ItemTag.origin == "genre")
        .order_by(ItemTag.tag)
    ).all()
    for source_id, tag in rows:
        genres.setdefault(source_id, []).append(tag)
    return genres
