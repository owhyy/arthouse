"""Map raw provider tags onto the closed descriptor vocabulary.

Run with: uv run python -m arthouse.tagging [--report] [--media MEDIA] [--unmapped N]

`item_descriptor` is derived data: `rebuild_descriptors` replaces it from
`item_tag` every run, so raw tags stay the source of truth. A tag that the
vocabulary does not know is dropped and counted, never guessed at — the
`--report` output is the worklist for adding aliases.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Engine, and_, create_engine, select, text
from sqlalchemy.orm import Session

from arthouse.config import get_settings
from arthouse.library import Base, Item, ItemDescriptor, ItemTag
from arthouse.logs import configure_logging
from arthouse.vocab import FACETS, Facet, lookup


@dataclass
class TaggingStats:
    tags: int  # raw tag rows considered
    mapped: int  # rows that resolved to a descriptor
    unmapped: int  # rows dropped because the vocabulary does not know the tag
    pairs: int  # distinct (item, descriptor) pairs written
    items: int  # items with at least one descriptor
    facets: dict[Facet, int]  # mapped rows per facet
    media: dict[str, tuple[int, int]]  # media type -> (considered, mapped)
    missing: dict[str, Counter[str]]  # media type -> unmapped tag -> count


def rebuild_descriptors(engine: Engine) -> TaggingStats:
    """Replace the derived `item_descriptor` rows from the raw `item_tag` rows.

    Both tables are dropped and recreated: `item_descriptor` lost its
    `media_type` key column, and the old `descriptor` facet cache is gone
    because facets now live in `arthouse.vocab`. Descriptors are regenerable,
    so no row migration is needed.
    """
    Base.metadata.tables["item_descriptor"].drop(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS descriptor"))
    Base.metadata.create_all(engine)

    pairs: set[tuple[str, str, str]] = set()
    owners: set[tuple[str, str]] = set()
    facets: Counter[Facet] = Counter()
    media: dict[str, tuple[int, int]] = {}
    missing: dict[str, Counter[str]] = defaultdict(Counter)
    considered = mapped_rows = 0

    with Session(engine) as db:
        rows = db.execute(
            select(
                Item.media_type,
                ItemTag.source,
                ItemTag.source_id,
                ItemTag.tag,
            ).join(
                Item,
                and_(
                    Item.source == ItemTag.source,
                    Item.source_id == ItemTag.source_id,
                ),
            )
        ).all()

        seen: Counter[str] = Counter()
        hit: Counter[str] = Counter()
        for media_type, source, source_id, tag in rows:
            considered += 1
            seen[media_type] += 1
            descriptor = lookup(tag)
            if descriptor is None:
                missing[media_type][tag] += 1
                continue
            mapped_rows += 1
            hit[media_type] += 1
            facets[descriptor.facet] += 1
            pairs.add((source, source_id, descriptor.name))
            owners.add((source, source_id))
        media = {name: (seen[name], hit[name]) for name in seen}

        db.add_all(
            ItemDescriptor(source=source, source_id=source_id, name=name)
            for source, source_id, name in sorted(pairs)
        )
        db.commit()

    return TaggingStats(
        tags=considered,
        mapped=mapped_rows,
        unmapped=considered - mapped_rows,
        pairs=len(pairs),
        items=len(owners),
        facets=dict(facets),
        media=media,
        missing=dict(missing),
    )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Map raw provider tags onto the descriptor vocabulary."
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="also print per-medium coverage and the most common unmapped tags",
    )
    parser.add_argument(
        "--media",
        choices=("music", "film", "book"),
        help="limit the unmapped report to one medium",
    )
    parser.add_argument(
        "--unmapped",
        type=int,
        default=25,
        help="how many unmapped tags to list per medium (default: 25)",
    )
    args = parser.parse_args()
    report = cast(bool, args.report)
    only = cast(str | None, args.media)
    limit = cast(int, args.unmapped)

    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.data_dir / 'library.db'}")
    stats = rebuild_descriptors(engine)

    share = 100 * stats.mapped / stats.tags if stats.tags else 0.0
    print(f"tags {stats.tags}  mapped {stats.mapped} ({share:.1f}%)  unmapped {stats.unmapped}")
    print(f"pairs {stats.pairs}  items {stats.items}")
    print("facets " + "  ".join(f"{facet} {stats.facets.get(facet, 0)}" for facet in FACETS))
    if not report:
        return

    for media in ("music", "film", "book"):
        if only is not None and media != only:
            continue
        counts = stats.media.get(media)
        if counts is None:
            continue
        considered, hit = counts
        if considered == 0:
            continue
        pct = 100 * hit / considered
        print(f"\n{media}: {hit}/{considered} tags mapped ({pct:.1f}%)")
        common = stats.missing.get(media, Counter()).most_common(limit)
        for tag, count in common:
            print(f"  {count:3}  {tag}")


if __name__ == "__main__":
    main()
