"""Page routes."""

from fastapi import APIRouter, HTTPException, Request

from arthouse.deps import DbSession, templates
from arthouse.library import (
    MEDIA_LABELS,
    MEDIA_ORDER,
    FilmMeta,
    query_film_directors,
    query_film_genres,
    query_film_meta,
    query_item,
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
    if item.media_type == "film":
        enriched = query_film_meta(db, item.source, item.source_id)
        if enriched is not None:
            film_meta, genres = enriched
    return templates.TemplateResponse(
        request,
        "library_detail.html",
        {"title": item.title, "item": item, "film_meta": film_meta, "genres": genres},
    )


@router.get("/library")
async def library(request: Request, db: DbSession, tab: str = "music"):
    if tab not in MEDIA_ORDER:
        raise HTTPException(status_code=404)
    items = query_items(db)
    counts = {media: sum(1 for i in items if i.media_type == media) for media in MEDIA_ORDER}
    tabs = [
        {
            "key": media,
            "label": MEDIA_LABELS[media],
            "count": counts[media],
            "active": media == tab,
        }
        for media in MEDIA_ORDER
    ]
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "title": f"Library · {MEDIA_LABELS[tab]}",
            "tabs": tabs,
            "items": [i for i in items if i.media_type == tab],
            "film_directors": query_film_directors(db) if tab == "film" else {},
            "film_genres": query_film_genres(db) if tab == "film" else {},
            "total": len(items),
        },
    )
