"""Page routes."""

from fastapi import APIRouter, Request

from arthouse.deps import templates

router = APIRouter()


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"title": "arthouse"})
