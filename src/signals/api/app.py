"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..bus.redis_bus import RedisSubscriber
from ..config import Settings
from ..store.pg import PgStore
from . import deps
from .routes_feed import router as feed_router
from .routes_meta import router as meta_router
from .ws import hub
from .ws import router as ws_router

STATIC = Path(__file__).resolve().parents[3] / "web" / "dist"


async def _connect_with_retry(dsn: str, attempts: int = 30) -> PgStore:
    """A healthy container is not a ready pool, so retry rather than crash-loop."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await PgStore.connect(dsn)
        except Exception as exc:  # noqa: BLE001 -- any connect failure is worth retrying
            last = exc
            await asyncio.sleep(min(0.5 * (attempt + 1), 3.0))
    raise RuntimeError(f"could not reach Postgres after {attempts} attempts: {last}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The API never calls SEC, so it must not require a contact address.
    settings = Settings.from_env(require_sec_user_agent=False)
    deps.set_settings(settings)
    store = await _connect_with_retry(settings.database_url)
    deps.set_store(store)
    hub.start(RedisSubscriber(settings.redis_url))
    try:
        yield
    finally:
        await hub.stop()
        await store.close()
        deps.set_store(None)


def create_app() -> FastAPI:
    app = FastAPI(title="Signals", version="0.1.0", lifespan=lifespan)

    # The dev UI runs on Vite's port; in production it is served from /.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(feed_router, prefix="/api")
    app.include_router(meta_router, prefix="/api")
    app.include_router(ws_router)

    # Serve the built UI when there is one, so `make up` is the whole story.
    if STATIC.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC / "index.html")

    return app


app = create_app()


def serve() -> int:
    import uvicorn

    uvicorn.run(
        "signals.api.app:app",
        host="0.0.0.0",  # noqa: S104 -- inside a container, published deliberately
        port=8000,
        reload="--reload" in sys.argv,
    )
    return 0
