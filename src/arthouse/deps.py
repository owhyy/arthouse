"""Shared dependencies."""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from arthouse.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_db_path(request: Request) -> Path:
    # Starlette types app/state dynamically, so basedpyright sees Any here.
    return request.app.state.db_path  # pyright: ignore[reportAny]


DbPath = Annotated[Path, Depends(get_db_path)]
