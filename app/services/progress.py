"""Onboarding and the completion estimate.

The estimate is deliberately a calendar *walk* rather than
`ceil(remaining / goal)`. Division gets Shabbat wrong: Friday carries a double
portion, Saturday carries none, so a naive 7-day rate is right only if the user
reports every Shabbat. Walking the calendar also makes it trivial to honour
per-user rest days later without rewriting the maths.

Cost is a few hundred iterations for a long tractate - cheaper than the round
trip that delivers it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    DayKind,
    PlanStatus,
    StudyPlan,
    Tractate,
    User,
    UserInventory,
    UserStats,
)
from app.services.calendar import classify_day, location_for, required_units_for
from app.services.study import StudyError
from app.services.zmanim import Location

#: Guard against goal=1 on a 1000-mishnah tractate producing an infinite walk.
MAX_PROJECTION_DAYS = 365 * 10


@dataclass(slots=True)
class Projection:
    estimated_end_date: date
    study_days: int
    calendar_days: int
    units_per_week: int


def project_completion(
    *,
    remaining_units: int,
    daily_goal: int,
    start_date: date,
    location: Location,
    observes_shabbat: bool,
    observes_yom_tov: bool = True,
    credits_shabbat: bool = True,
) -> Projection:
    """When will they finish?

    `credits_shabbat=False` models a user who does not do the Friday double
    portion - their Saturdays contribute nothing, so the estimate stretches.
    """
    if remaining_units <= 0:
        return Projection(start_date, 0, 0, 0)
    if daily_goal <= 0:
        raise StudyError("invalid_goal", "daily goal must be at least 1")

    done = 0
    done_before = 0
    study_days = 0
    end_date = start_date

    for offset in range(MAX_PROJECTION_DAYS):
        cursor = start_date + timedelta(days=offset)
        kind = classify_day(cursor, location, observes_shabbat, observes_yom_tov)

        capacity = required_units_for(kind, daily_goal)
        if kind == DayKind.EREV_SHABBAT and not credits_shabbat:
            capacity = daily_goal  # they only do Friday's own portion
        if kind == DayKind.YOM_TOV:
            capacity = 0

        if capacity:
            done_before = done
            done += capacity
            study_days += 1

        if done >= remaining_units:
            end_date = cursor
            # Friday's quota is Friday's *and* Shabbat's. If the last units
            # come out of Shabbat's half, the siyum belongs on Shabbat - that
            # is the day the ledger credits, and the day the user celebrates.
            if (
                kind == DayKind.EREV_SHABBAT
                and credits_shabbat
                and done_before + daily_goal < remaining_units
            ):
                end_date = cursor + timedelta(days=1)
            break
    else:  # pragma: no cover - only reachable with absurd inputs
        raise StudyError("projection_overflow", "goal too small to ever finish")

    return Projection(
        estimated_end_date=end_date,
        study_days=study_days,
        calendar_days=(end_date - start_date).days + 1,
        units_per_week=daily_goal * 7,
    )


def create_plan(
    session: Session,
    user: User,
    *,
    tractate_slug: str,
    daily_goal: int,
    start_date: date | None = None,
    now: datetime | None = None,
) -> tuple[StudyPlan, Projection]:
    now = now or clock.now()
    start_date = start_date or now.date()

    tractate = session.execute(
        select(Tractate).where(Tractate.slug == tractate_slug)
    ).scalar_one_or_none()
    if tractate is None:
        raise StudyError("unknown_tractate", f"no tractate {tractate_slug!r}", 404)

    existing = session.execute(
        select(StudyPlan).where(
            StudyPlan.user_id == user.id, StudyPlan.status == PlanStatus.ACTIVE
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise StudyError(
            "plan_exists", "finish or abandon the current plan first", 409
        )

    projection = project_completion(
        remaining_units=tractate.mishnayot_count,
        daily_goal=daily_goal,
        start_date=start_date,
        location=location_for(user),
        observes_shabbat=user.observes_shabbat,
        observes_yom_tov=user.observes_yom_tov,
    )

    plan = StudyPlan(
        user_id=user.id,
        tractate_id=tractate.id,
        daily_goal=daily_goal,
        start_date=start_date,
        estimated_end_date=projection.estimated_end_date,
        current_ordinal=0,
        status=PlanStatus.ACTIVE,
    )
    session.add(plan)

    if session.get(UserStats, user.id) is None:
        session.add(UserStats(user_id=user.id))
    session.flush()
    return plan, projection


def change_goal(
    session: Session, user: User, plan: StudyPlan, new_goal: int, today: date
) -> Projection:
    """Re-project from *today* with the remaining units.

    Today's quota is updated too, but only while the day is still open. A day
    that has already been resolved keeps the requirement it was judged
    against — re-scoring a finished day is how you accidentally hand out (or
    revoke) a streak someone already earned.
    """
    from app.models import DayStatus, StudyDay
    from app.services.calendar import required_units_for

    tractate = session.get(Tractate, plan.tractate_id)
    plan.daily_goal = new_goal

    open_today = session.execute(
        select(StudyDay).where(
            StudyDay.user_id == user.id,
            StudyDay.local_date == today,
            StudyDay.status.in_([DayStatus.PENDING, DayStatus.SHABBAT_PENDING]),
        )
    ).scalar_one_or_none()
    if open_today is not None:
        open_today.required_units = required_units_for(
            open_today.day_kind, new_goal
        )
    projection = project_completion(
        remaining_units=tractate.mishnayot_count - plan.current_ordinal,
        daily_goal=new_goal,
        start_date=today,
        location=location_for(user),
        observes_shabbat=user.observes_shabbat,
        observes_yom_tov=user.observes_yom_tov,
    )
    plan.estimated_end_date = projection.estimated_end_date
    return projection


def set_position(
    session: Session, plan: StudyPlan, *, chapter: int, mishnah: int
) -> int:
    """Move the reading cursor to a specific chapter:mishnah.

    This changes *where you are reading*, not what you have been credited for.
    Day rows, points and streaks are untouched — someone who starts Berakhot at
    chapter 4 has not retroactively earned the first three chapters, and
    rewriting the ledger to pretend otherwise would corrupt the one table the
    whole economy trusts.
    """
    from app.models import Mishnah

    target = session.execute(
        select(Mishnah).where(
            Mishnah.tractate_id == plan.tractate_id,
            Mishnah.chapter == chapter,
            Mishnah.number == mishnah,
        )
    ).scalar_one_or_none()
    if target is None:
        raise StudyError(
            "no_such_mishnah", f"פרק {chapter} משנה {mishnah} לא קיימת במסכת", 404
        )

    # current_ordinal is "how many are behind me", so landing *on* a mishnah
    # means the cursor sits one before it.
    plan.current_ordinal = target.ordinal - 1
    return target.ordinal


def switch_tractate(
    session: Session,
    user: User,
    plan: StudyPlan | None,
    *,
    tractate_slug: str,
    daily_goal: int,
    start_chapter: int = 1,
    start_mishnah: int = 1,
    now: datetime | None = None,
) -> tuple[StudyPlan, Projection]:
    """Abandon the current plan and start a new one.

    The old plan is marked ABANDONED rather than deleted: its study_days and
    ledger entries still reference it, and the streak the user built while on
    it is real history.
    """
    now = now or clock.now()
    if plan is not None:
        plan.status = PlanStatus.ABANDONED
        session.flush()

    new_plan, projection = create_plan(
        session,
        user,
        tractate_slug=tractate_slug,
        daily_goal=daily_goal,
        now=now,
    )
    if (start_chapter, start_mishnah) != (1, 1):
        set_position(session, new_plan, chapter=start_chapter, mishnah=start_mishnah)

    # Re-project from the real position, and let today's open row pick up the
    # new goal so the switch takes effect immediately rather than tomorrow.
    projection = change_goal(session, user, new_plan, daily_goal, now.date())
    return new_plan, projection


def chapter_structure(session: Session, tractate: Tractate) -> list[dict]:
    """[{chapter, mishnayot, first_ordinal}] — drives the chapter picker."""
    from sqlalchemy import func as sa_func

    from app.models import Mishnah

    rows = session.execute(
        select(
            Mishnah.chapter,
            sa_func.count(Mishnah.id),
            sa_func.min(Mishnah.ordinal),
        )
        .where(Mishnah.tractate_id == tractate.id)
        .group_by(Mishnah.chapter)
        .order_by(Mishnah.chapter)
    ).all()
    return [
        {"chapter": c, "mishnayot": n, "first_ordinal": first}
        for c, n, first in rows
    ]
