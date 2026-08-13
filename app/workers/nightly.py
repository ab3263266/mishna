"""Scheduled settlement.

Runs **hourly**, not nightly, despite the module name: users are spread across
timezones, and each one's day rolls over at their own local 03:00. An hourly
pass catches every rollover within the hour.

The job is a safety net, not the primary path - `settle_user` is already called
on every read. It exists so that a user who stops opening the app still gets
their penalties recorded (otherwise a lapsed user's streak would sit frozen at
40 forever), and so leaderboards are not computed from stale rows.

Deploy as a Kubernetes CronJob / Celery beat task / `APScheduler` entry:

    0 * * * *  python -m app.workers.nightly
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.core import clock
from app.models import PlanStatus, StudyPlan, User, UserStats
from app.services import settlement

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def run(now: datetime | None = None) -> dict:
    now = now or clock.now()
    processed = resolved = errors = 0

    with SessionLocal() as session:
        for user in _candidates(session, now):
            try:
                outcome = settlement.settle_user(session, user, now)
                session.commit()
                processed += 1
                resolved += len(outcome.days_resolved)
            except Exception:
                # One poisoned account must not stall the batch.
                session.rollback()
                errors += 1
                logger.exception("settlement failed for user %s", user.id)

    summary = {"processed": processed, "days_resolved": resolved, "errors": errors}
    logger.info("settlement sweep %s", summary)
    return summary


def _candidates(session: Session, now: datetime) -> list[User]:
    """Users who plausibly have an unsettled day.

    The date filter is deliberately loose (UTC-yesterday) rather than exact:
    computing each user's local rollover in SQL would mean a function call per
    row and no index. `settle_user` is idempotent, so over-selecting costs a
    cheap no-op and under-selecting would lose data.
    """
    cutoff = (now - timedelta(days=1)).date()
    stmt = (
        select(User)
        .join(UserStats, UserStats.user_id == User.id)
        .join(StudyPlan, StudyPlan.user_id == User.id)
        .where(
            User.deleted_at.is_(None),
            StudyPlan.status == PlanStatus.ACTIVE,
            (UserStats.last_settled_date.is_(None))
            | (UserStats.last_settled_date < cutoff),
        )
        .limit(BATCH_SIZE)
    )
    return list(session.execute(stmt).scalars())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
