"""Page routes."""

from fastapi import APIRouter, HTTPException, Request

from arthouse.deps import DbSession, templates
from arthouse.library import MEDIA_LABELS, MEDIA_ORDER, primary_genres, split_genres
from arthouse.models import GenreRow, ItemMeta
from arthouse.queries import (
    count_items,
    count_items_by_media,
    query_descriptor_options,
    query_item,
    query_item_credits,
    query_item_descriptor_groups,
    query_item_genres,
    query_item_meta,
    query_items,
)
from arthouse.vocab import lookup

router = APIRouter()


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"title": "arthouse"})


@router.get("/library/{item_id}")
async def library_detail(request: Request, item_id: int, db: DbSession):
    item = query_item(db, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    item_meta: ItemMeta | None = None
    genres: list[GenreRow] = []
    enriched = query_item_meta(db, item.source, item.source_id)
    if enriched is not None:
        item_meta, genres = enriched
    primary_genres, secondary_genres = split_genres(genres)
    descriptor_groups = query_item_descriptor_groups(db, item.source, item.source_id)
    return templates.TemplateResponse(
        request,
        "library_detail.html",
        {
            "title": item.title,
            "item": item,
            "item_meta": item_meta,
            "primary_genres": primary_genres,
            "secondary_genres": secondary_genres,
            "descriptor_groups": descriptor_groups,
        },
    )


PAGE_SIZE = 60


@router.get("/library")
async def library(
    request: Request,
    db: DbSession,
    tab: str = "music",
    q: str = "",
    descriptor: str = "",
    page: int = 1,
):
    if tab not in MEDIA_ORDER:
        raise HTTPException(status_code=404)
    search = q.strip()
    # An alias in the URL ("melancholy") resolves to its descriptor
    # ("melancholic"); an unknown value is kept as-is so it matches nothing
    # rather than silently turning the filter off.
    requested = descriptor.strip()
    hit = lookup(requested) if requested else None
    selected = hit.name if hit is not None else requested
    per_media = count_items_by_media(db)
    counts = {media: per_media.get(media, 0) for media in MEDIA_ORDER}
    total_matches = count_items(
        db,
        media_type=tab,
        search=search or None,
        descriptor=selected or None,
    )
    page_count = max(1, (total_matches + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), page_count)
    items = query_items(
        db,
        media_type=tab,
        search=search or None,
        descriptor=selected or None,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    filtered = bool(search or selected)
    tabs = [
        {
            "key": media,
            "label": MEDIA_LABELS[media],
            # While filtering, the active tab shows the number of matches;
            # the other tabs keep their library totals.
            "count": total_matches if filtered and media == tab else counts[media],
            "active": media == tab,
        }
        for media in MEDIA_ORDER
    ]
    descriptor_groups = query_descriptor_options(db, tab)
    descriptor_names = {option.name for _, options in descriptor_groups for option in options}
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "title": f"Library · {MEDIA_LABELS[tab]}",
            "tab": tab,
            "q": search,
            "descriptor": selected,
            "descriptor_groups": descriptor_groups,
            "descriptor_listed": selected in descriptor_names,
            "tabs": tabs,
            "page": page,
            "page_count": page_count,
            "items": items,
            "item_credits": query_item_credits(db, tab),
            "item_genres": {
                source_id: primary_genres(genres)
                for source_id, genres in query_item_genres(
                    db, tab, [item.source_id for item in items]
                ).items()
            },
            "total": sum(counts.values()),
        },
    )
