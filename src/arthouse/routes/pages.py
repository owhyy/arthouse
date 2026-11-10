"""Page routes."""

from fastapi import APIRouter, HTTPException, Request

from arthouse.deps import DbPath, templates
from arthouse.library import MEDIA_LABELS, MEDIA_ORDER, query_item, query_items

router = APIRouter()


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"title": "arthouse"})


@router.get("/library/{item_id}")
async def library_detail(request: Request, item_id: int, db_path: DbPath):
    item = query_item(db_path, item_id)
    if item is None or item.id is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "library_detail.html", {"title": item.title, "item": item}
    )


@router.get("/library")
async def library(request: Request, db_path: DbPath):
    items = query_items(db_path)
    groups = [
        {
            "label": MEDIA_LABELS[media],
            "count": sum(1 for i in items if i.media_type == media),
            "items": [i for i in items if i.media_type == media],
        }
        for media in MEDIA_ORDER
    ]
    return templates.TemplateResponse(
        request, "library.html", {"title": "Library", "groups": groups, "total": len(items)}
    )
