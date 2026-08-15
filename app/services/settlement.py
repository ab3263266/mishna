"""The settlement engine.

One function, `settle_user`, owns every transition in the game economy. It
walks the user's *closed* local days in order and finalises each one exactly
once. Nothing else in the codebase is allowed to touch streaks or penalties.

Why this shape:

* **Idempotent.** Re-running it changes nothing. It is safe to call from a
  request handler on every read, from an hourly cron, and from a support
  script - simultaneously.
* **Lazy + eager.** Calling it on read means a user who opens the app after two
  weeks sees the correct state immediately. The cron exists only so that users
  who *never* open the app still get their penalties recorded, and so the
  leaderboard is not stale.
* **Ordered.** Days are resolved chronologically because the multiplier depends
  on the streak, which depends on the previous day.

The one rule that surprises people: settlement never finalises *today*. Today
is still winnable.
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import Settings, get_settings
from app.models import (
    TERMINAL_STATUSES,
    CreditSource,
    DayKind,
    DayStatus,
    FreezeUsage,
    PlanStatus,
    StudyDay,
    StudyPlan,
    TxnType,
    User,
    UserInventory,
    UserStats,
)
from app.services import ledger
from app.services.calendar import UserClock, classify_day, required_units_for
from app.services.scoring import ScoringRules, penalty_for_miss, points_for_completion

logger = logging.getLogger(__name__)

STREAK_FREEZE_SKU = "streak_freeze"


class Decision(enum.StrEnum):
    CARRY = "carry"  # already terminal, nothing to do
    CREDIT = "credit"
    MISS = "miss"
    FREEZE = "freeze"
    REST = "rest"
    EXEMPT = "exempt"


@dataclass(slots=True)
class SettlementContext:
    user: User
    plan: StudyPlan
    stats: UserStats
    clock: UserClock
    rules: ScoringRules
    settings: Settings
    now: datetime


@dataclass(slots=True)
class SettlementOutcome:
    days_resolved: list[tuple[date, DayStatus]] = field(default_factory=list)
    points_delta: int = 0
    freezes_used: int = 0
    streak: int = 0
    stopped_reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.days_resolved)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def settle_user(
    session: Session,
    user: User,
    now: datetime | None = None,
    *,
    rules: ScoringRules | None = None,
) -> SettlementOutcome:
    now = now or clock.now()
    settings = get_settings()
    outcome = SettlementOutcome()

    plan = session.execute(
        select(StudyPlan).where(
            StudyPlan.user_id == user.id, StudyPlan.status == PlanStatus.ACTIVE
        )
    ).scalar_one_or_none()
    if plan is None:
        outcome.stopped_reason = "no_active_plan"
        return outcome

    # Serialise every scoring write for this user behind one row lock.
    stats = ledger.lock_stats(session, user.id)

    ctx = SettlementContext(
        user=user,
        plan=plan,
        stats=stats,
        clock=UserClock.for_user(user, settings.day_rollover_hour),
        rules=rules or _rules_from_settings(settings),
        settings=settings,
        now=now,
    )

    today = ctx.clock.local_date(now)
    start_balance = stats.total_points

    cursor = stats.last_settled_date or (plan.start_date - timedelta(days=1))
    cursor += timedelta(days=1)

    processed = 0
    while cursor < today:
        if processed >= settings.max_days_per_settlement:
            outcome.stopped_reason = "max_days_reached"
            break

        day = get_or_create_day(session, ctx, cursor)
        decision = _decide(session, ctx, day)

        _apply(session, ctx, day, decision)
        if decision is not Decision.CARRY:
            outcome.days_resolved.append((cursor, day.status))
        if decision is Decision.FREEZE:
            outcome.freezes_used += 1

        stats.last_settled_date = cursor
        cursor += timedelta(days=1)
        processed += 1

    # Today's row must exist so the client can render "0 / 4 today", and so a
    # rest day is visible as one from the moment it starts.
    if plan.status == PlanStatus.ACTIVE:
        get_or_create_day(session, ctx, today)

    outcome.points_delta = stats.total_points - start_balance
    outcome.streak = stats.current_streak
    session.flush()
    return outcome


def _rules_from_settings(settings: Settings) -> ScoringRules:
    return ScoringRules(
        base_points=settings.base_points,
        miss_penalty=settings.miss_penalty,
        streak_freeze_cost=settings.streak_freeze_cost,
    )


# --------------------------------------------------------------------------- #
# Day rows
# --------------------------------------------------------------------------- #


def get_or_create_day(
    session: Session, ctx: SettlementContext, d: date
) -> StudyDay:
    day = session.execute(
        select(StudyDay).where(
            StudyDay.user_id == ctx.user.id, StudyDay.local_date == d
        )
    ).scalar_one_or_none()
    if day is not None:
        return day

    kind = classify_day(d, ctx.user.study_week)
    day = StudyDay(
        user_id=ctx.user.id,
        plan_id=ctx.plan.id,
        local_date=d,
        day_kind=kind,
        required_units=required_units_for(kind, ctx.plan.daily_goal),
        completed_units=0,
        status=DayStatus.PENDING,
    )
    session.add(day)
    session.flush()
    return day


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


def _decide(session: Session, ctx: SettlementContext, day: StudyDay) -> Decision:
    if day.status in TERMINAL_STATUSES:
        return Decision.CARRY
    if day.local_date < ctx.plan.start_date:
        return Decision.EXEMPT

    if day.day_kind is DayKind.REST_DAY:
        # A rest day is never punished. Credit it if they learned anyway - a
        # five-day learner who opens the app on Shabbat should be rewarded for
        # it, not told the day does not count.
        return (
            Decision.CREDIT
            if day.completed_units >= ctx.plan.daily_goal
            else Decision.REST
        )

    if day.required_units > 0 and day.completed_units >= day.required_units:
        return Decision.CREDIT

    if _freeze_available(session, ctx):
        return Decision.FREEZE

    return Decision.MISS


def _freeze_available(session: Session, ctx: SettlementContext) -> bool:
    inv = _inventory_row(session, ctx.user.id)
    return inv is not None and inv.quantity > 0


def _inventory_row(session: Session, user_id: uuid.UUID) -> UserInventory | None:
    stmt = ledger.maybe_for_update(
        session,
        select(UserInventory).where(
            UserInventory.user_id == user_id,
            UserInventory.sku == STREAK_FREEZE_SKU,
        ),
    )
    return session.execute(stmt).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Applying a decision
# --------------------------------------------------------------------------- #


def _apply(
    session: Session, ctx: SettlementContext, day: StudyDay, decision: Decision
) -> None:
    match decision:
        case Decision.CARRY:
            return
        case Decision.CREDIT:
            credit_day(session, ctx, day, source=day.credit_source or CreditSource.APP)
        case Decision.MISS:
            _miss_day(session, ctx, day)
        case Decision.FREEZE:
            _freeze_day(session, ctx, day)
        case Decision.REST:
            _neutral_day(ctx, day, DayStatus.REST_DAY)
        case Decision.EXEMPT:
            _neutral_day(ctx, day, DayStatus.EXEMPT)


def credit_day(
    session: Session,
    ctx: SettlementContext,
    day: StudyDay,
    *,
    source: CreditSource = CreditSource.APP,
) -> int:
    """Award a completed day. Shared by settlement and by live completion, so
    that finishing today's goal at 22:00 and having it credited by the cron at
    03:00 produce byte-identical rows."""
    if day.status in TERMINAL_STATUSES:
        return 0

    new_streak = ctx.stats.current_streak + 1
    points = points_for_completion(ctx.rules, new_streak)

    posted = ledger.post_transaction(
        session,
        ctx.stats,
        amount=points,
        txn_type=TxnType.DAILY_STUDY,
        idempotency_key=ledger.daily_key(ctx.user.id, day.local_date),
        related_date=day.local_date,
        meta={"streak": new_streak, "source": str(source)},
    )
    if posted is None:
        # Already awarded by a concurrent settlement; do not move the streak.
        logger.info("duplicate credit suppressed for %s %s", ctx.user.id, day.local_date)
        points = 0
    else:
        ctx.stats.current_streak = new_streak
        ctx.stats.longest_streak = max(ctx.stats.longest_streak, new_streak)

    day.status = DayStatus.COMPLETED
    day.credit_source = source
    day.points_awarded = points
    day.multiplier = float(ctx.rules.multiplier_for(new_streak))
    day.streak_after = ctx.stats.current_streak
    day.completed_at = day.completed_at or ctx.now
    day.settled_at = ctx.now
    return points


def _miss_day(session: Session, ctx: SettlementContext, day: StudyDay) -> None:
    penalty = penalty_for_miss(ctx.rules, ctx.stats.total_points)
    if penalty:
        ledger.post_transaction(
            session,
            ctx.stats,
            amount=-penalty,
            txn_type=TxnType.MISS_PENALTY,
            idempotency_key=ledger.penalty_key(ctx.user.id, day.local_date),
            related_date=day.local_date,
            meta={"streak_broken_from": ctx.stats.current_streak},
        )

    ctx.stats.current_streak = 0
    day.status = DayStatus.MISSED
    day.points_awarded = -penalty
    day.streak_after = 0
    day.settled_at = ctx.now


def _freeze_day(session: Session, ctx: SettlementContext, day: StudyDay) -> None:
    """Spend a Streak Freeze: streak survives, no penalty - but the streak does
    not advance either. A freeze protects, it does not substitute for study."""
    inv = _inventory_row(session, ctx.user.id)
    if inv is None or inv.quantity <= 0:  # lost a race; fall back to a miss
        _miss_day(session, ctx, day)
        return

    inv.quantity -= 1
    ctx.stats.freezes_consumed += 1
    session.add(FreezeUsage(user_id=ctx.user.id, covered_date=day.local_date))

    day.status = DayStatus.FROZEN_ITEM
    day.points_awarded = 0
    day.streak_after = ctx.stats.current_streak
    day.settled_at = ctx.now


def _neutral_day(ctx: SettlementContext, day: StudyDay, status: DayStatus) -> None:
    day.status = status
    day.points_awarded = 0
    day.streak_after = ctx.stats.current_streak
    day.settled_at = ctx.now


def build_context(
    session: Session, user: User, now: datetime, plan: StudyPlan, stats: UserStats
) -> SettlementContext:
    """Context builder for callers that already hold the lock (study.py, shop.py)."""
    settings = get_settings()
    return SettlementContext(
        user=user,
        plan=plan,
        stats=stats,
        clock=UserClock.for_user(user, settings.day_rollover_hour),
        rules=_rules_from_settings(settings),
        settings=settings,
        now=now,
    )
