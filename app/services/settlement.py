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
  on the streak, which depends on the previous day. Friday must resolve before
  Saturday, or the Shabbat double credit scores at the wrong tier.

The one rule that surprises people: settlement never finalises *today*. Today
is still winnable.
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

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
    ShabbatReport,
    StudyDay,
    StudyPlan,
    TxnType,
    User,
    UserInventory,
    UserStats,
)
from app.services import ledger
from app.services.calendar import (
    UserClock,
    classify_day,
    location_for,
    required_units_for,
    shabbat_report_deadline,
)
from app.services.scoring import ScoringRules, penalty_for_miss, points_for_completion
from app.services.zmanim import (
    FRIDAY,
    Location,
    SATURDAY,
    ZmanimLibraryProvider,
    ZmanimProvider,
    enclosing_shabbat_friday,
    window_for_moment,
)

logger = logging.getLogger(__name__)

STREAK_FREEZE_SKU = "streak_freeze"


class Decision(enum.StrEnum):
    CARRY = "carry"  # already terminal, nothing to do
    CREDIT = "credit"
    MISS = "miss"
    FREEZE = "freeze"
    NEUTRAL_SHABBAT = "neutral_shabbat"
    EXEMPT = "exempt"
    DEFER = "defer"  # not resolvable yet - stop the walk here


@dataclass(slots=True)
class SettlementContext:
    user: User
    plan: StudyPlan
    stats: UserStats
    clock: UserClock
    location: Location
    provider: ZmanimProvider
    rules: ScoringRules
    settings: Settings
    now: datetime
    #: True when `now` sits inside a Shabbat/Yom Tov rest window. Spec 3.2: the
    #: penalty timer pauses, so no day may be finalised as MISSED right now.
    in_rest_window: bool = False


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
    provider: ZmanimProvider | None = None,
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

    location = location_for(user)
    provider = provider or ZmanimLibraryProvider(
        pre_freeze_minutes=settings.pre_shabbat_freeze_minutes
    )
    ctx = SettlementContext(
        user=user,
        plan=plan,
        stats=stats,
        clock=UserClock.for_location(location, settings.day_rollover_hour),
        location=location,
        provider=provider,
        rules=rules or _rules_from_settings(settings),
        settings=settings,
        now=now,
    )
    ctx.in_rest_window = (
        user.observes_shabbat
        and window_for_moment(provider, location, now) is not None
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

        if decision is Decision.DEFER:
            outcome.stopped_reason = f"deferred_at_{cursor.isoformat()}"
            break

        _apply(session, ctx, day, decision)
        if decision is not Decision.CARRY:
            outcome.days_resolved.append((cursor, day.status))
        if decision is Decision.FREEZE:
            outcome.freezes_used += 1

        # Only advance the watermark for days we actually finalised. A deferred
        # Friday must be revisited, so the watermark stays behind it.
        stats.last_settled_date = cursor
        cursor += timedelta(days=1)
        processed += 1

    # Today's row must exist so the client can render "0 / 4 today" and so the
    # Friday double portion is visible from Friday morning (spec 3.1).
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
        motash_bonus=settings.motash_bonus,
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

    kind = classify_day(
        d, ctx.location, ctx.user.observes_shabbat, ctx.user.observes_yom_tov
    )
    day = StudyDay(
        user_id=ctx.user.id,
        plan_id=ctx.plan.id,
        local_date=d,
        day_kind=kind,
        required_units=required_units_for(kind, ctx.plan.daily_goal),
        completed_units=0,
        status=(
            DayStatus.SHABBAT_PENDING
            if kind in (DayKind.EREV_SHABBAT, DayKind.SHABBAT)
            and ctx.user.observes_shabbat
            else DayStatus.PENDING
        ),
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

    goal_met = day.required_units > 0 and day.completed_units >= day.required_units

    if ctx.user.observes_shabbat and day.day_kind in (
        DayKind.EREV_SHABBAT,
        DayKind.SHABBAT,
    ):
        return _decide_shabbat(session, ctx, day, goal_met)

    if day.day_kind == DayKind.YOM_TOV:
        # Yom Tov is never punished. Credit it if the user did log study
        # (some users keep only Shabbat), otherwise hold the streak steady.
        return (
            Decision.CREDIT
            if day.completed_units >= ctx.plan.daily_goal
            else Decision.NEUTRAL_SHABBAT
        )

    if goal_met:
        return Decision.CREDIT

    # Spec 3.2: while the user is inside the Shabbat window, the penalty timer
    # is paused. A Thursday left unfinished waits until Motzash to be judged.
    if ctx.in_rest_window:
        return Decision.DEFER

    if _freeze_available(session, ctx):
        return Decision.FREEZE

    return Decision.MISS


def _decide_shabbat(
    session: Session, ctx: SettlementContext, day: StudyDay, goal_met: bool
) -> Decision:
    """Friday and Saturday resolve as a pair, gated on the Motzash report."""
    saturday = (
        day.local_date
        if day.day_kind == DayKind.SHABBAT
        else day.local_date + timedelta(days=1)
    )
    report = session.execute(
        select(ShabbatReport).where(
            ShabbatReport.user_id == ctx.user.id,
            ShabbatReport.shabbat_date == saturday,
        )
    ).scalar_one_or_none()

    if report is not None and report.completed:
        return Decision.CREDIT

    if day.day_kind == DayKind.EREV_SHABBAT and goal_met:
        # They logged the full double portion on Friday morning, in-app.
        return Decision.CREDIT

    if day.day_kind == DayKind.SHABBAT and _friday_double_portion_logged(
        session, ctx, saturday - timedelta(days=1)
    ):
        # Product decision: logging Friday's double portion *is* doing
        # Saturday's quota, so Saturday credits without the checkbox. The
        # checkbox is still the only way to earn the Motash bonus.
        return Decision.CREDIT

    deadline = shabbat_report_deadline(
        ctx.clock, saturday, ctx.settings.shabbat_report_grace_days
    )
    if ctx.now < deadline:
        return Decision.DEFER

    # Spec 3.3: no login over Shabbat costs neither the streak nor points.
    return Decision.NEUTRAL_SHABBAT


def _friday_double_portion_logged(
    session: Session, ctx: SettlementContext, friday: date
) -> bool:
    fri = session.execute(
        select(StudyDay).where(
            StudyDay.user_id == ctx.user.id, StudyDay.local_date == friday
        )
    ).scalar_one_or_none()
    return (
        fri is not None
        and fri.required_units > 0
        and fri.completed_units >= fri.required_units
    )


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
        case Decision.NEUTRAL_SHABBAT:
            _neutral_day(ctx, day, DayStatus.SHABBAT_UNREPORTED)
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
    txn_type = (
        TxnType.SHABBAT_STUDY
        if day.day_kind in (DayKind.EREV_SHABBAT, DayKind.SHABBAT)
        else TxnType.DAILY_STUDY
    )

    posted = ledger.post_transaction(
        session,
        ctx.stats,
        amount=points,
        txn_type=txn_type,
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
    location = location_for(user)
    provider = ZmanimLibraryProvider(
        pre_freeze_minutes=settings.pre_shabbat_freeze_minutes
    )
    ctx = SettlementContext(
        user=user,
        plan=plan,
        stats=stats,
        clock=UserClock.for_location(location, settings.day_rollover_hour),
        location=location,
        provider=provider,
        rules=_rules_from_settings(settings),
        settings=settings,
        now=now,
    )
    ctx.in_rest_window = (
        user.observes_shabbat and window_for_moment(provider, location, now) is not None
    )
    return ctx
