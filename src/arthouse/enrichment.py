"""Policy shared by the provider enrichment passes.

Films and music differ in matching and payloads, but they agree on how an item
is identified in reports and on what a strict no-match means: a finished
attempt that a rerun would repeat, so it is remembered instead of retried.
"""

from sqlalchemy.orm import Session

from arthouse.models import Item, ItemMiss


def item_label(item: Item) -> str:
    """One-line identification for progress and miss reports; title alone collides."""
    label = f"{item.title} — {item.creator}" if item.creator else item.title
    return f"{label} ({item.year})" if item.year is not None else label


def record_miss(db: Session, item: Item, provider: str) -> bool:
    """Remember a strict no-match so a rerun does not search the same query.

    Returns True when the row was written, False when it was already there.
    Delete the row to have the item tried again.
    """
    if db.get(ItemMiss, (item.source, item.source_id, provider)) is not None:
        return False
    db.add(ItemMiss(source=item.source, source_id=item.source_id, provider=provider))
    db.commit()
    return True
