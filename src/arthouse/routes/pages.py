"""Page routes."""

from fastapi import APIRouter, HTTPException, Request

from arthouse.deps import DbSession, templates
from arthouse.library import (
    MEDIA_LABELS,
    MEDIA_ORDER,
    FilmMeta,
    query_film_directors,
    query_film_genres,
    query_film_keywords,
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
    keywords: list[str] = []
    if item.media_type == "film":
        enriched = query_film_meta(db, item.source, item.source_id)
        if enriched is not None:
            film_meta, genres = enriched
            keywords = query_film_keywords(db, item.source, item.source_id)
    return templates.TemplateResponse(
        request,
        "library_detail.html",
        {
            "title": item.title,
            "item": item,
            "film_meta": film_meta,
            "genres": genres,
            "keywords": keywords,
        },
    )


@router.get("/library")
async def library(request: Request, db: DbSession, tab: str = "music", q: str = ""):
    if tab not in MEDIA_ORDER:
        raise HTTPException(status_code=404)
    search = q.strip()
    all_items = query_items(db)
    counts = {media: sum(1 for i in all_items if i.media_type == media) for media in MEDIA_ORDER}
    items = query_items(db, media_type=tab, search=search or None)
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
            "tab": tab,
            "q": search,
            "tabs": tabs,
            "items": items,
            "film_directors": query_film_directors(db) if tab == "film" else {},
            "film_genres": query_film_genres(db) if tab == "film" else {},
            "total": len(all_items),
        },
    )
