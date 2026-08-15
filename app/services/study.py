"""Study logging and the home-screen read model."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    TERMINAL_STATUSES,
    CreditSource,
    DayKind,
    DayStatus,
    Mishnah,
    PlanStatus,
    StudyEvent,
    StudyPlan,
    Tractate,
    User,
)
from app.services import ledger, settlement
from app.services.scoring import next_tier_preview, points_for_completion

logger = logging.getLogger(__name__)


class StudyError(Exception):
    """Raised for user-correctable conditions; mapped to 4xx in the API layer."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True)
class LogResult:
    units_logged: int
    day_completed: bool
    points_awarded: int
    streak: int
    total_points: int
    plan_completed: bool
    next_ordinal: int


def local_today(user: User, now: datetime | None = None) -> date:
    """The user's current study date. Never `datetime.now().date()` - that is
    a UTC date, and a learner in Jerusalem at 01:00 is still on yesterday."""
    from app.core.config import get_settings
    from app.services.calendar import UserClock

    return UserClock.for_user(user, get_settings().day_rollover_hour).local_date(
        now or clock.now()
    )


def goal_for_day(day, plan: StudyPlan) -> int:
    """How many units credit this day.

    A rest day requires nothing, but a learner who reads anyway should get the
    day - so the bar there is the plain daily goal rather than the day's (zero)
    requirement.
    """
    return day.required_units if day.required_units > 0 else plan.daily_goal


# --------------------------------------------------------------------------- #
# Logging study
# --------------------------------------------------------------------------- #


def log_study(
    session: Session,
    user: User,
    units: int,
    *,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> LogResult:
    if units <= 0:
        raise StudyError("invalid_units", "units must be positive")

    now = now or clock.now()

    # Bring the account current first: yesterday's penalty must land before
    # today's points, or a returning user briefly sees an inflated streak.
    settlement.settle_user(session, user, now)

    plan = _active_plan(session, user)
    stats = ledger.lock_stats(session, user.id)
    ctx = settlement.build_context(session, user, now, plan, stats)

    if idempotency_key:
        existing = session.execute(
            select(StudyEvent).where(StudyEvent.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return _result_from_state(session, ctx, existing.credited_local_date, 0)

    today = ctx.clock.local_date(now)
    day = settlement.get_or_create_day(session, ctx, today)

    if day.status in TERMINAL_STATUSES and day.status != DayStatus.COMPLETED:
        raise StudyError(
            "day_closed", f"day {today} is already resolved as {day.status}", 409
        )

    tractate = session.get(Tractate, plan.tractate_id)
    remaining_in_tractate = tractate.mishnayot_count - plan.current_ordinal
    if remaining_in_tractate <= 0:
        raise StudyError("plan_complete", "this tractate is already finished", 409)
    units = min(units, remaining_in_tractate)

    session.add(
        StudyEvent(
            user_id=user.id,
            plan_id=plan.id,
            credited_local_date=today,
            units=units,
            from_ordinal=plan.current_ordinal,
            to_ordinal=plan.current_ordinal + units,
            source=CreditSource.APP,
            idempotency_key=idempotency_key,
        )
    )
    plan.current_ordinal += units
    stats.total_units_completed += units
    day.completed_units += units
    day.first_logged_at = day.first_logged_at or now

    points = 0
    completed_now = False
    if (
        day.status not in TERMINAL_STATUSES
        and day.completed_units >= goal_for_day(day, plan)
    ):
        # Credit immediately - waiting for the nightly job to hand out points
        # for work already done makes the app feel broken.
        points = settlement.credit_day(session, ctx, day, source=CreditSource.APP)
        completed_now = True

    plan_completed = plan.current_ordinal >= tractate.mishnayot_count
    if plan_completed:
        plan.status = PlanStatus.COMPLETED
        plan.completed_at = now

    session.flush()
    return LogResult(
        units_logged=units,
        day_completed=completed_now,
        points_awarded=points,
        streak=stats.current_streak,
        total_points=stats.total_points,
        plan_completed=plan_completed,
        next_ordinal=plan.current_ordinal + 1,
    )


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #


def today_state(
    session: Session, user: User, now: datetime | None = None
) -> dict:
    """Everything the home screen needs, after bringing the account current."""
    now = now or clock.now()
    settlement.settle_user(session, user, now)

    plan = _active_plan(session, user)
    stats = ledger.lock_stats(session, user.id)
    ctx = settlement.build_context(session, user, now, plan, stats)

    today = ctx.clock.local_date(now)
    day = settlement.get_or_create_day(session, ctx, today)
    tractate = session.get(Tractate, plan.tractate_id)

    tier_hint = next_tier_preview(ctx.rules, stats.current_streak)

    return {
        "local_date": today,
        "day_kind": day.day_kind,
        "study_week": user.study_week,
        "required_units": day.required_units,
        "completed_units": day.completed_units,
        "is_rest_day": day.day_kind == DayKind.REST_DAY,
        "optional_goal": goal_for_day(day, plan),
        "status": day.status,
        "points_if_completed": points_for_completion(
            ctx.rules, stats.current_streak + 1
        ),
        "current_streak": stats.current_streak,
        "longest_streak": stats.longest_streak,
        "total_points": stats.total_points,
        "next_tier": (
            {"days_away": tier_hint[0], "multiplier": float(tier_hint[1])}
            if tier_hint
            else None
        ),
        "progress": {
            "tractate": tractate.name_he,
            "completed": plan.current_ordinal,
            "total": tractate.mishnayot_count,
            "next_ordinal": plan.current_ordinal + 1,
            "next_ref": _mishnah_ref(session, plan, plan.current_ordinal + 1),
            "daily_goal": plan.daily_goal,
            "estimated_end_date": plan.estimated_end_date,
        },
    }


def _mishnah_ref(session: Session, plan: StudyPlan, ordinal: int) -> str | None:
    """Human address for a running ordinal, e.g. 'פרק ג׳, משנה ב׳'."""
    mishnah = session.execute(
        select(Mishnah).where(
            Mishnah.tractate_id == plan.tractate_id, Mishnah.ordinal == ordinal
        )
    ).scalar_one_or_none()
    if mishnah is None:
        return None
    return f"פרק {mishnah.chapter}, משנה {mishnah.number}"


def _active_plan(session: Session, user: User) -> StudyPlan:
    plan = session.execute(
        select(StudyPlan).where(
            StudyPlan.user_id == user.id, StudyPlan.status == PlanStatus.ACTIVE
        )
    ).scalar_one_or_none()
    if plan is None:
        raise StudyError("no_active_plan", "no active study plan", 409)
    return plan


def _result_from_state(
    session: Session, ctx: settlement.SettlementContext, d: date, units: int
) -> LogResult:
    day = settlement.get_or_create_day(session, ctx, d)
    return LogResult(
        units_logged=units,
        day_completed=day.status == DayStatus.COMPLETED,
        points_awarded=day.points_awarded,
        streak=ctx.stats.current_streak,
        total_points=ctx.stats.total_points,
        plan_completed=ctx.plan.status == PlanStatus.COMPLETED,
        next_ordinal=ctx.plan.current_ordinal + 1,
    )
