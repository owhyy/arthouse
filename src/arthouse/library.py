"""Unified library from platform exports: parse CSVs, store in SQLite."""

import csv
import sqlite3
from datetime import date
from pathlib import Path
from typing import cast

from pydantic import BaseModel

MEDIA_ORDER = ("music", "film", "book")
MEDIA_LABELS = {"music": "Music", "film": "Films", "book": "Books"}


class LibraryItem(BaseModel):
    id: int | None = None  # SQLite rowid, populated on query
    media_type: str  # music | film | book
    source: str  # letterboxd | rateyourmusic | goodreads
    source_id: str
    title: str
    creator: str
    year: int | None
    rating: float  # normalized to 0-5
    rating_native: str
    rated_on: date | None

    @property
    def score(self) -> str:
        return score_str(self.rating)


def score_str(rating: float) -> str:
    """Display a 0-5 rating as a 0-10 score.

    x2 maps all three platforms losslessly: RYM 1-10 stays integer,
    Letterboxd half-stars become integers, Goodreads quarter steps become
    halves — so one decimal is always exact and never false precision.
    """
    return f"{rating * 2:g}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        # RYM headers contain stray spaces (", First Name"); keys are stripped.
        # Rows with more values than headers get them under a None key as a
        # list — dropped.
        return [
            {k.strip(): v.strip() for k, v in row.items() if k is not None and isinstance(v, str)}
            for row in csv.DictReader(f)
        ]


def _parse_letterboxd(path: Path) -> list[LibraryItem]:
    items: list[LibraryItem] = []
    for row in _read_csv(path):
        rating = (row.get("Rating") or "").strip()
        if not rating:
            continue
        items.append(
            LibraryItem(
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


def _parse_rym(path: Path) -> list[LibraryItem]:
    items: list[LibraryItem] = []
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
            LibraryItem(
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


def _parse_goodreads(path: Path) -> list[LibraryItem]:
    items: list[LibraryItem] = []
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
            LibraryItem(
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


def build_db(db_path: Path, data_dir: Path) -> int:
    """(Re)build the items table from data_dir exports; returns item count.

    Missing export files are skipped, so an empty data dir yields an empty
    library (CI has no data/).
    """
    parsers = {
        "letterboxd/ratings.csv": _parse_letterboxd,
        "rateyourmusic": _parse_rym,
        "goodreads/goodreads_library_export.csv": _parse_goodreads,
    }
    items: list[LibraryItem] = []
    for name, parse in parsers.items():
        # The RYM export filename contains the username; match on directory.
        paths = [data_dir / name] if "/" in name else sorted(data_dir.glob(f"{name}/*.csv"))
        for path in paths:
            if path.is_file():
                items.extend(parse(path))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS items")
    conn.execute(
        """CREATE TABLE items (
               id INTEGER PRIMARY KEY,
               media_type TEXT, source TEXT, source_id TEXT, title TEXT,
               creator TEXT, year INTEGER, rating REAL, rating_native TEXT,
               rated_on TEXT)"""
    )
    conn.executemany(
        """INSERT INTO items (media_type, source, source_id, title, creator,
                              year, rating, rating_native, rated_on)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                i.media_type,
                i.source,
                i.source_id,
                i.title,
                i.creator,
                i.year,
                i.rating,
                i.rating_native,
                i.rated_on.isoformat() if i.rated_on else None,
            )
            for i in items
        ],
    )
    conn.commit()
    conn.close()
    return len(items)


def _item_from_row(r: sqlite3.Row) -> LibraryItem:
    # Row fields are typed Any; model_validate coerces and checks each column.
    return LibraryItem.model_validate(dict(r))


def query_items(db_path: Path, media_type: str | None = None) -> list[LibraryItem]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Cursor.fetchall is typed list[Any]; the schema above guarantees Rows.
    rows = cast(
        "list[sqlite3.Row]",
        conn.execute(
            """SELECT id, media_type, source, source_id, title, creator, year,
                      rating, rating_native, rated_on
               FROM items
               WHERE (? IS NULL OR media_type = ?)
               ORDER BY rating DESC, title COLLATE NOCASE""",
            (media_type, media_type),
        ).fetchall(),
    )
    conn.close()
    return [_item_from_row(r) for r in rows]


def query_item(db_path: Path, item_id: int) -> LibraryItem | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = cast(
        "sqlite3.Row | None",
        conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone(),
    )
    conn.close()
    return _item_from_row(row) if row is not None else None
