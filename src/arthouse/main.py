"""Application factory and server entry point."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine

from arthouse.assets import asset_url, build_asset_map
from arthouse.config import BASE_DIR, Settings, get_settings
from arthouse.deps import templates
from arthouse.library import build_db
from arthouse.routes import pages
from arthouse.tagging import rebuild_descriptors


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    # echo=debug prints every SQL statement to stderr (the server terminal).
    engine = create_engine(f"sqlite:///{data_dir / 'library.db'}", echo=settings.debug)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # Building here rather than at import time keeps `arthouse.main`
        # importable without touching the database: tests create their own app
        # on a throwaway directory and importing the module stays side-effect free.
        build_db(engine, data_dir)
        # Descriptors are derived from item_tag; rebuild so the detail page
        # and the library filter never serve a stale vocabulary.
        rebuild_descriptors(engine)
        yield

    app = FastAPI(
        title="arthouse",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    static_dir = BASE_DIR / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    app.state.engine = engine

    templates.env.globals["asset"] = partial(  # pyright: ignore[reportArgumentType]
        asset_url, static_dir, build_asset_map(static_dir), debug=settings.debug
    )
    app.include_router(pages.router)

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "arthouse.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        reload_includes=["*.html"] if settings.debug else None,
    )
