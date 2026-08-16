"""Which mishnayot make up today's portion.

Every case here is a bug that reached the running app first.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import _portion_ordinals
from app.models import Base, DayKind, DayStatus, StudyDay, StudyEvent, StudyPlan

TODAY = date(2026, 8, 20)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def make_plan(session: Session, *, current_ordinal: int, goal: int) -> StudyPlan:
    plan = StudyPlan(
        user_id=uuid.uuid4(),
        tractate_id=1,
        daily_goal=goal,
        start_date=TODAY,
        current_ordinal=current_ordinal,
    )
    session.add(plan)
    session.flush()
    return plan


def make_day(session: Session, plan: StudyPlan, *, required: int, completed: int,
             plan_id=None, status=DayStatus.PENDING) -> StudyDay:
    day = StudyDay(
        user_id=plan.user_id,
        plan_id=plan_id or plan.id,
        local_date=TODAY,
        day_kind=DayKind.WEEKDAY,
        required_units=required,
        completed_units=completed,
        status=status,
    )
    session.add(day)
    session.flush()
    return day


def log_event(session: Session, plan: StudyPlan, units: int, to_ordinal: int) -> None:
    session.add(
        StudyEvent(
            user_id=plan.user_id,
            plan_id=plan.id,
            credited_local_date=TODAY,
            units=units,
            from_ordinal=to_ordinal - units,
            to_ordinal=to_ordinal,
        )
    )
    session.flush()


def test_fresh_day_starts_at_the_cursor(session):
    plan = make_plan(session, current_ordinal=10, goal=2)
    day = make_day(session, plan, required=2, completed=0)
    assert _portion_ordinals(session, plan, day) == [11, 12]


def test_the_whole_portion_stays_visible_after_logging(session):
    """Ticking off a mishnah must not make it disappear from the screen."""
    plan = make_plan(session, current_ordinal=12, goal=2)
    day = make_day(session, plan, required=2, completed=2)
    log_event(session, plan, 2, to_ordinal=12)
    assert _portion_ordinals(session, plan, day) == [11, 12]


def test_partial_progress_keeps_the_anchor(session):
    plan = make_plan(session, current_ordinal=11, goal=3)
    day = make_day(session, plan, required=3, completed=1)
    log_event(session, plan, 1, to_ordinal=11)
    assert _portion_ordinals(session, plan, day) == [11, 12, 13]


def test_switching_tractate_mid_day_does_not_rewind_the_new_one(session):
    """The day row still holds units learned in the *old* tractate. Subtracting
    those rewound the new tractate's portion - switching to Megillah 2:1 showed
    Megillah 1:10 instead."""
    old_plan = make_plan(session, current_ordinal=20, goal=2)
    log_event(session, old_plan, 2, to_ordinal=20)          # learned in the old tractate
    day = make_day(session, old_plan, required=2, completed=2,
                   status=DayStatus.COMPLETED)

    new_plan = make_plan(session, current_ordinal=11, goal=3)  # Megillah, at 2:1
    assert _portion_ordinals(session, new_plan, day) == [12, 13, 14]


def test_a_day_row_from_another_plan_uses_the_current_goal(session):
    """The old day's requirement must not decide how many of the new
    tractate's mishnayot to preview."""
    old_plan = make_plan(session, current_ordinal=20, goal=8)
    day = make_day(session, old_plan, required=8, completed=8,
                   status=DayStatus.COMPLETED)

    new_plan = make_plan(session, current_ordinal=0, goal=3)
    assert len(_portion_ordinals(session, new_plan, day)) == 3


def test_learning_past_the_quota_keeps_the_extra_on_screen(session):
    """Finishing the day does not close the tractate. Someone who carries on
    has those mishnayot counted against the plan, so a portion pinned to the
    quota would drop them the moment they were logged."""
    plan = make_plan(session, current_ordinal=13, goal=2)
    day = make_day(session, plan, required=2, completed=3,
                   status=DayStatus.COMPLETED)
    log_event(session, plan, 2, to_ordinal=12)   # the quota
    log_event(session, plan, 1, to_ordinal=13)   # one more, past it

    assert _portion_ordinals(session, plan, day) == [11, 12, 13]


def test_a_rest_day_still_previews_the_daily_goal(session):
    """A rest day requires nothing, but the portion stays on screen: reading it
    is optional, not forbidden, and an empty study tab reads as broken."""
    plan = make_plan(session, current_ordinal=10, goal=2)
    day = make_day(session, plan, required=0, completed=0)
    assert _portion_ordinals(session, plan, day) == [11, 12]
