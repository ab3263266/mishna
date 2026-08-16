from __future__ import annotations

import pathlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: The project root, resolved from this file rather than the working directory.
#: A relative SQLite path ("./mishnah.db") silently creates a *different*
#: database for every directory you launch from - including whatever folder a
#: dev-server wrapper happens to run in. Anchoring it here means one database,
#: inside this project, no matter where uvicorn is started.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "mishnah.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    #: SQLite by default so `uvicorn app.main:app` works with zero setup.
    #: Point at PostgreSQL for anything real - see README.
    database_url: str = f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"

    #: Unlocks /api/v1/dev/* : passwordless login, time travel, reset.
    #: Defaults to OFF. `dev_login` mints a session for any email address and
    #: `dev/time` moves the clock for the whole process, so this defaulting to
    #: True would mean one forgotten environment variable is a full account
    #: takeover. Local development opts in via .env instead.
    dev_mode: bool = False

    #: Where the browser lands after a successful Google sign-in.
    public_base_url: str = "http://localhost:8010"

    #: Set false only when serving over plain HTTP locally; the refresh-token
    #: cookie is otherwise marked Secure.
    cookie_secure: bool = True

    #: Hourly settlement sweep inside the web process. OFF by default because
    #: nothing in the app currently needs it: `settle_user` runs on every read,
    #: so a user who returns after three weeks is settled correctly the moment
    #: they open the app. Turn it on when something has to be correct *without*
    #: the user showing up - a leaderboard, or "your streak breaks in 3 hours"
    #: notifications.
    run_scheduler: bool = False

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    #: 32+ bytes: PyJWT warns below the RFC 7518 minimum for HMAC-SHA256.
    #: Override in .env for anything but local development.
    jwt_secret: str = "dev-only-secret-not-for-production-use"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    base_points: int = 10
    miss_penalty: int = 15
    streak_freeze_cost: int = 120

    #: A day ends at 03:00 local, not midnight: study logged at 01:30 belongs
    #: to the evening that just ended.
    day_rollover_hour: int = 3

    # Safety valve: a user returning after two years should not make the
    # settlement loop walk 700 days inside a request.
    max_days_per_settlement: int = 400


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.database_url = normalise_database_url(settings.database_url)
    return settings


def normalise_database_url(url: str) -> str:
    """Point a bare `postgresql://` URL at psycopg 3.

    Every managed Postgres hands out `postgresql://…`, and SQLAlchemy reads
    that as psycopg **2**, which is not installed - so the app dies at boot
    with `No module named 'psycopg2'`. Copying the connection string from the
    provider is the obvious thing to do, so it should be the thing that works.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url
