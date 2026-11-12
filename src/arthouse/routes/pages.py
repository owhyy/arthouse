"""Page routes."""

from fastapi import APIRouter, HTTPException, Request

from arthouse.deps import DbSession, templates
from arthouse.descriptors import canonical
from arthouse.library import (
    MEDIA_LABELS,
    MEDIA_ORDER,
    FilmMeta,
    query_descriptor_options,
    query_film_directors,
    query_film_genres,
    query_film_meta,
    query_item,
    query_item_descriptor_groups,
    query_items,
)

router = APIRouter()


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"title": "arthouse"})


@router.get("/library/{item_id}")
async def library_detail(request: Request, item_id: int, db: DbSession):
    item = query_item(db, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    film_meta: FilmMeta | None = None
    genres: list[str] = []
    descriptor_groups: list[tuple[str, list[str]]] = []
    if item.media_type == "film":
        enriched = query_film_meta(db, item.source, item.source_id)
        if enriched is not None:
            film_meta, genres = enriched
        descriptor_groups = query_item_descriptor_groups(db, item.source, item.source_id)
    return templates.TemplateResponse(
        request,
        "library_detail.html",
        {
            "title": item.title,
            "item": item,
            "film_meta": film_meta,
            "genres": genres,
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
    selected = canonical(descriptor) if tab == "film" and descriptor.strip() else ""
    all_items = query_items(db)
    counts = {media: sum(1 for i in all_items if i.media_type == media) for media in MEDIA_ORDER}
    matches = query_items(
        db,
        media_type=tab,
        search=search or None,
        descriptor=selected or None,
    )
    page_count = max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), page_count)
    items = matches[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    filtered = bool(search or selected)
    tabs = [
        {
            "key": media,
            "label": MEDIA_LABELS[media],
            # While filtering, the active tab shows the number of matches;
            # the other tabs keep their library totals.
            "count": len(matches) if filtered and media == tab else counts[media],
            "active": media == tab,
        }
        for media in MEDIA_ORDER
    ]
    descriptor_groups = query_descriptor_options(db) if tab == "film" else []
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
            "film_directors": query_film_directors(db) if tab == "film" else {},
            "film_genres": query_film_genres(db) if tab == "film" else {},
            "total": len(all_items),
        },
    )
