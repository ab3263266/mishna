from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    **({} if _is_sqlite else {"pool_size": 10, "max_overflow": 20}),
)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        """SQLite's defaults are wrong for a web app in three separate ways.

        * `busy_timeout=0` means a second writer fails *instantly* with
          "database is locked" instead of waiting. A background cache warm
          running alongside a request is enough to trigger it.
        * `journal_mode=DELETE` makes readers block writers. WAL lets them run
          concurrently, which is the whole point here.
        * `foreign_keys=OFF` — SQLite does not enforce foreign keys unless you
          ask, so every `ON DELETE CASCADE` in the schema is silently inert.

        None of this applies to PostgreSQL, which gets all three right.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def get_session() -> Iterator[Session]:
    """One transaction per request.

    Committing here rather than inside the services keeps the settlement engine
    composable: `report_shabbat` can call `settle_user` and both land in the
    same transaction, so a crash halfway cannot leave points awarded but the
    streak un-advanced.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
