from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from app.api.auth_google import router as google_router
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

    # Fail fast on a configuration that would be unsafe on a public host.
    from app.core.preflight import check

    check(settings)

    # Convenience for local SQLite only: one command and the app works. On
    # PostgreSQL the container runs `alembic upgrade head` before boot, and
    # create_all here would fight it.
    if settings.database_url.startswith("sqlite"):
        from app.db.seed import run as seed

        logger.info("seeding: %s", seed())

    from app.workers.scheduler import start, stop

    sweep = start(app)
    try:
        yield
    finally:
        await stop(sweep)


app = FastAPI(title="Mishnah Tracker API", version="1.0.0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")
app.include_router(google_router, prefix="/api/v1")

if get_settings().dev_mode:
    from app.api.dev import router as dev_router

    app.include_router(dev_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# --------------------------------------------------------------------------- #
# Legal pages
# --------------------------------------------------------------------------- #

LEGAL_DIR = WEB_DIR / "legal"
LEGAL_PAGES = ("privacy", "terms", "accessibility", "credits")


def _legal_page(name: str) -> HTMLResponse:
    """Serve a legal page with the operator's details filled in.

    The contact address is substituted at request time rather than written
    into the files, so it lives in one place and can be set per deployment.
    Left unset it renders visibly blank rather than as a plausible-looking
    address that nobody reads.
    """
    settings = get_settings()
    html = (LEGAL_DIR / f"{name}.html").read_text(encoding="utf-8")
    html = html.replace("{{owner_name}}", settings.owner_name or "— טרם הוגדר —")
    html = html.replace("{{contact_email}}", settings.contact_email or "— טרם הוגדר —")
    return HTMLResponse(html)


for _page in LEGAL_PAGES:
    app.add_api_route(
        f"/{_page}",
        (lambda name: lambda: _legal_page(name))(_page),
        methods=["GET"],
        include_in_schema=False,
        response_class=HTMLResponse,
    )


@app.get("/legal.css", include_in_schema=False)
def legal_css() -> FileResponse:
    return FileResponse(LEGAL_DIR / "legal.css", media_type="text/css")
