"""Study logging and the Motzash Shabbat report."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

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
    ShabbatReport,
    StudyDay,
    StudyEvent,
    StudyPlan,
    Tractate,
    TxnType,
    User,
)
from app.services import ledger, settlement
from app.services.calendar import shabbat_report_deadline
from app.services.scoring import next_tier_preview, points_for_completion
from app.services.zmanim import SATURDAY, window_for_moment

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
        and day.required_units > 0
        and day.completed_units >= day.required_units
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
# The Motzash checkbox
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ShabbatReportResult:
    friday_points: int
    shabbat_points: int
    motash_bonus: int
    streak: int
    total_points: int
    units_credited: int


def report_shabbat(
    session: Session,
    user: User,
    *,
    completed: bool = True,
    now: datetime | None = None,
) -> ShabbatReportResult:
    """Spec 3.4-3.6: "I completed my study over Shabbat".

    Credits Friday and Saturday in that order - the order matters, because the
    second day scores at the higher tier the first one just unlocked.
    """
    now = now or clock.now()

    plan = _active_plan(session, user)
    stats = ledger.lock_stats(session, user.id)
    ctx = settlement.build_context(session, user, now, plan, stats)

    saturday = _reportable_saturday(ctx, now)
    friday = saturday - timedelta(days=1)

    if session.execute(
        select(ShabbatReport).where(
            ShabbatReport.user_id == user.id, ShabbatReport.shabbat_date == saturday
        )
    ).scalar_one_or_none():
        raise StudyError("already_reported", "this Shabbat was already reported", 409)

    # Optional bonus (spec 3.7): only before real local midnight on Motzash.
    earned_bonus = completed and now < ctx.clock.civil_midnight(saturday)

    session.add(
        ShabbatReport(
            user_id=user.id,
            shabbat_date=saturday,
            friday_date=friday,
            completed=completed,
            reported_at=now,
            earned_motash_bonus=earned_bonus,
        )
    )
    session.flush()

    if not completed:
        # An honest "no" is not a punishment: the pair goes neutral, exactly as
        # if they had never opened the app.
        outcome = settlement.settle_user(session, user, now)
        return ShabbatReportResult(0, 0, 0, stats.current_streak, stats.total_points, 0)

    friday_day = settlement.get_or_create_day(session, ctx, friday)
    shabbat_day = settlement.get_or_create_day(session, ctx, saturday)

    units_credited = _backfill_units(session, ctx, plan, friday_day, now)

    friday_points = settlement.credit_day(
        session, ctx, friday_day, source=CreditSource.SHABBAT_REPORT
    )
    shabbat_points = settlement.credit_day(
        session, ctx, shabbat_day, source=CreditSource.SHABBAT_REPORT
    )

    bonus = 0
    if earned_bonus:
        posted = ledger.post_transaction(
            session,
            stats,
            amount=ctx.rules.motash_bonus,
            txn_type=TxnType.MOTASH_BONUS,
            idempotency_key=ledger.motash_key(user.id, saturday),
            related_date=saturday,
            meta={"reported_at": now.isoformat()},
        )
        bonus = ctx.rules.motash_bonus if posted else 0

    # Advance the watermark past the now-resolved Friday.
    settlement.settle_user(session, user, now)
    session.flush()

    return ShabbatReportResult(
        friday_points=friday_points,
        shabbat_points=shabbat_points,
        motash_bonus=bonus,
        streak=stats.current_streak,
        total_points=stats.total_points,
        units_credited=units_credited,
    )


def _reportable_saturday(ctx: settlement.SettlementContext, now: datetime) -> date:
    """The Shabbat this report covers, or an error explaining why not.

    Open from havdalah until the end of the grace period (default: end of
    Sunday's study day).
    """
    today = ctx.clock.local_date(now)
    offset = (today.weekday() - SATURDAY) % 7
    saturday = today - timedelta(days=offset)

    window = ctx.provider.shabbat_window(saturday - timedelta(days=1), ctx.location)
    if now < window.end:
        raise StudyError("shabbat_not_over", "Shabbat has not ended yet", 409)

    deadline = shabbat_report_deadline(
        ctx.clock, saturday, ctx.settings.shabbat_report_grace_days
    )
    if now >= deadline:
        raise StudyError(
            "report_window_closed",
            f"the report window for {saturday} closed at {deadline.isoformat()}",
            409,
        )
    return saturday


def _backfill_units(
    session: Session,
    ctx: settlement.SettlementContext,
    plan: StudyPlan,
    friday_day: StudyDay,
    now: datetime,
) -> int:
    """Advance the reading cursor for the double portion the user learned
    offline - only for the part they did not already log in-app on Friday."""
    missing = max(friday_day.required_units - friday_day.completed_units, 0)
    if missing == 0:
        return 0

    tractate = session.get(Tractate, plan.tractate_id)
    missing = min(missing, tractate.mishnayot_count - plan.current_ordinal)
    if missing <= 0:
        return 0

    session.add(
        StudyEvent(
            user_id=ctx.user.id,
            plan_id=plan.id,
            credited_local_date=friday_day.local_date,
            units=missing,
            from_ordinal=plan.current_ordinal,
            to_ordinal=plan.current_ordinal + missing,
            source=CreditSource.SHABBAT_REPORT,
            idempotency_key=f"shabbat:{ctx.user.id}:{friday_day.local_date}",
        )
    )
    plan.current_ordinal += missing
    ctx.stats.total_units_completed += missing
    friday_day.completed_units += missing
    return missing


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

    window = (
        window_for_moment(ctx.provider, ctx.location, now)
        if user.observes_shabbat
        else None
    )
    upcoming = (
        ctx.provider.shabbat_window(_next_friday(today), ctx.location)
        if user.observes_shabbat
        else None
    )

    can_report, saturday = _can_report_shabbat(session, ctx, now)
    tier_hint = next_tier_preview(ctx.rules, stats.current_streak)

    return {
        "local_date": today,
        "day_kind": day.day_kind,
        "required_units": day.required_units,
        "completed_units": day.completed_units,
        "is_double_portion": day.day_kind == DayKind.EREV_SHABBAT,
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
        "penalties_frozen": window is not None,
        "rest_window": (
            {
                "kind": window.kind,
                "onset": window.onset,
                "ends": window.end,
            }
            if window
            else None
        ),
        "next_shabbat_freeze_at": upcoming.freeze_start if upcoming else None,
        "can_report_shabbat": can_report,
        "reportable_shabbat_date": saturday,
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


def _can_report_shabbat(
    session: Session, ctx: settlement.SettlementContext, now: datetime
) -> tuple[bool, date | None]:
    try:
        saturday = _reportable_saturday(ctx, now)
    except StudyError:
        return False, None
    already = session.execute(
        select(ShabbatReport).where(
            ShabbatReport.user_id == ctx.user.id,
            ShabbatReport.shabbat_date == saturday,
        )
    ).scalar_one_or_none()
    return already is None, saturday


def _next_friday(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7)


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
