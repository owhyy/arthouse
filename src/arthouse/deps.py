"""Shared dependencies."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from arthouse.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_db(request: Request) -> Iterator[Session]:
    # Starlette types app/state dynamically, so basedpyright sees Any here.
    engine: Engine = request.app.state.engine  # pyright: ignore[reportAny]
    with Session(engine) as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
