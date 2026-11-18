"""Unified library from platform exports: parse CSVs, store in SQLite."""

import csv
import html
import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from arthouse.db import create_schema
from arthouse.models import Base, GenreRow, Item

MEDIA_ORDER = ("music", "film", "book")
MEDIA_LABELS = {"music": "Music", "film": "Films", "book": "Books"}

# Display genres carry the provider's vote tally so the UI can rank them. The
# primary cut is a ratio of the best-voted genre rather than a fixed number,
# because vote totals differ by an order of magnitude between albums; below two
# votes there is no signal, so every genre stays primary and nothing is hidden.
PRIMARY_GENRE_RATIO = 0.4
PRIMARY_GENRE_MIN_COUNT = 2
PRIMARY_GENRE_MAX = 4
# Only the next few best-voted genres are worth showing under "More genres":
SECONDARY_GENRE_MAX = 3


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


def _creator_name(first: str, last: str) -> str:
    """Join the export's split name columns into the name a page shows."""
    first, last = html.unescape(first).strip(), html.unescape(last).strip()
    return f"{first} {last}" if first and last else (last or first)


def _parse_rym(path: Path) -> list[Item]:
    items: list[Item] = []
    for row in _read_csv(path):
        rating = (row.get("Rating") or "").strip()
        if rating in ("", "0"):
            continue
        # RYM rates 1-10. It stores each artist twice: `First/Last Name` in the
        # artist's own script and `First/Last Name localized` in RYM's Latin
        # form. The Latin form stays the card's primary name; the original is
        # kept so the credit line can show both. RYM escapes HTML entities
        # (`&amp;`, `&#34;`) in name and title columns; stored text is the text
        # a page shows.
        original = _creator_name(row["First Name"], row["Last Name"])
        creator = _creator_name(
            row["First Name localized"] or row["First Name"],
            row["Last Name localized"] or row["Last Name"],
        )
        release_year = row["Release_Date"].strip()[:4]
        items.append(
            Item(
                media_type="music",
                source="rateyourmusic",
                source_id=row["RYM Album"],
                title=html.unescape(row["Title"]),
                creator=creator,
                creator_original="" if original == creator else original,
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
    create_schema(engine)
    with Session(engine) as session:
        session.add_all(items)
        session.commit()
    return len(items)


def split_genres(genres: Sequence[GenreRow]) -> tuple[list[GenreRow], list[GenreRow]]:
    """Split ranked genres into primary and secondary for display.

    Input must be in rank order. A genre is primary when its tally is at least
    `PRIMARY_GENRE_RATIO` of the top genre's and it has at least
    `PRIMARY_GENRE_MIN_COUNT` votes, capped at `PRIMARY_GENRE_MAX` so a cluster
    of equally-voted sub-genres cannot take over the header. The remaining
    genres are capped at `SECONDARY_GENRE_MAX`, because past the next few the
    provider's tail is mostly noise. Providers without votes leave every count
    at zero, which falls through to "all primary" and keeps their display
    unchanged.
    """
    if not genres:
        return [], []
    top = genres[0].count
    if top < PRIMARY_GENRE_MIN_COUNT:
        return list(genres), []
    cut = max(PRIMARY_GENRE_MIN_COUNT, math.ceil(top * PRIMARY_GENRE_RATIO))
    # Ranked input makes the qualifying genres a prefix, so the cap can slice.
    primary = [genre for genre in genres if genre.count >= cut][:PRIMARY_GENRE_MAX]
    secondary = list(genres[len(primary) :])[:SECONDARY_GENRE_MAX]
    return primary, secondary


def primary_genres(genres: Sequence[GenreRow]) -> list[GenreRow]:
    """The main genres of one item; the card grid shows only these."""
    return split_genres(genres)[0]
