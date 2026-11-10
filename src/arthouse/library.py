"""Unified library from platform exports: parse CSVs, store in SQLite."""

# SQLAlchemy declarative columns are initialized by the ORM's instrumentation,
# which pyright cannot see — the rule is false-positives on this module only.
# pyright: reportUninitializedInstanceVariable=false

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

MEDIA_ORDER = ("music", "film", "book")
MEDIA_LABELS = {"music": "Music", "film": "Films", "book": "Books"}


class Base(DeclarativeBase):
    pass


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

    @property
    def score(self) -> str:
        return score_str(self.rating)


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
            )
        )
    return items


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

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(items)
        session.commit()
    return len(items)


def query_items(db: Session, media_type: str | None = None) -> list[Item]:
    stmt = select(Item).order_by(Item.rating.desc(), Item.title.collate("nocase"))
    if media_type is not None:
        stmt = stmt.where(Item.media_type == media_type)
    return list(db.scalars(stmt))


def query_item(db: Session, item_id: int) -> Item | None:
    return db.get(Item, item_id)
