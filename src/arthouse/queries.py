"""Read queries over the library schema.

Keeping them apart from `arthouse.library` means a page or CLI can import the
query it needs without dragging in the CSV parsers (and vice versa).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from arthouse.models import GenreRow, Item, ItemDescriptor, ItemGenre, ItemMeta
from arthouse.vocab import FACETS, Facet, facet_of


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
