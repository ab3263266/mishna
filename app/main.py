from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import router
from app.core.config import get_settings

logger = logging.getLogger(__name__)
WEB_DIR = pathlib.Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and seed reference data on boot.

    Fine for SQLite development. On PostgreSQL you want Alembic owning the
    schema instead - `create_all` cannot express a migration, only a fresh
    install. The seeder itself is idempotent either way.
    """
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        from app.db.seed import run as seed

        logger.info("seeding: %s", seed())
    yield


app = FastAPI(title="Mishnah Tracker API", version="1.0.0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")

if get_settings().dev_mode:
    from app.api.dev import router as dev_router

    app.include_router(dev_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
