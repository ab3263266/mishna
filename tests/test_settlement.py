"""Integration tests for the settlement engine.

Runs on SQLite by default so `pytest` works with no setup. Point
TEST_DATABASE_URL at PostgreSQL to run the same suite against the real target:

    TEST_DATABASE_URL=postgresql+psycopg://localhost/mishnah_test pytest tests/

The two dialects differ in ways this suite deliberately does not paper over:
SQLite takes a database-wide write lock instead of `SELECT ... FOR UPDATE`, so
these tests prove the *rules* are right but cannot prove the *locking* is.
Concurrency needs the Postgres run.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    DayStatus,
    StudyDay,
    ShopItem,
    StudyPlan,
    StudyWeek,
    Tractate,
    User,
    UserInventory,
    UserStats,
)
from app.services import settlement, study

TEST_DB = os.getenv("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")

JERUSALEM = ZoneInfo("Asia/Jerusalem")

SUNDAY = date(2026, 8, 9)
MONDAY = date(2026, 8, 10)
FRIDAY = date(2026, 8, 14)
SATURDAY = date(2026, 8, 15)


def at(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=JERUSALEM)


@pytest.fixture
def session() -> Session:
    engine = create_engine(TEST_DB)
    if not engine.url.get_backend_name() == "sqlite":
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        s.add(
            Tractate(
                id=1,
                slug="berakhot",
                name_he="ברכות",
                name_en="Berakhot",
                seder="zeraim",
                order_index=1,
                chapter_count=9,
                mishnayot_count=57,
            )
        )
        s.add(
            ShopItem(
                sku="streak_freeze", name_he="הקפאת רצף", cost_points=120, max_owned=3
            )
        )
        s.commit()
        yield s


def make_user(session: Session, week: StudyWeek, start: date = MONDAY) -> User:
    u = User(
        google_sub=f"sub-{uuid.uuid4()}",
        email="a@example.com",
        timezone="Asia/Jerusalem",
        study_week=week,
    )
    session.add(u)
    session.flush()
    session.add(UserStats(user_id=u.id, total_points=0))
    session.add(StudyPlan(user_id=u.id, tractate_id=1, daily_goal=2, start_date=start))
    session.commit()
    return u


@pytest.fixture
def user(session: Session) -> User:
    """The default learner studies all seven days."""
    return make_user(session, StudyWeek.SEVEN_DAYS)


@pytest.fixture
def five_day_user(session: Session) -> User:
    return make_user(session, StudyWeek.FIVE_DAYS, start=SUNDAY)


def settle(session, user, moment):
    return settlement.settle_user(session, user, moment)


def log(session, user, units, moment):
    return study.log_study(session, user, units, now=moment)


def statuses(session, user) -> dict[date, str]:
    return {
        d.local_date: d.status
        for d in session.execute(
            select(StudyDay).where(StudyDay.user_id == user.id)
        ).scalars()
    }


# --------------------------------------------------------------------------- #
# Points and the multiplier
# --------------------------------------------------------------------------- #


def test_fourth_consecutive_day_pays_the_multiplier(session, user):
    for offset in range(4):
        result = log(session, user, 2, at(MONDAY + timedelta(days=offset), 9))
        expected = 10 if offset < 3 else 15
        assert result.points_awarded == expected, f"day {offset + 1}"
        assert result.streak == offset + 1

    stats = session.get(UserStats, user.id)
    assert stats.total_points == 45  # 10 + 10 + 10 + 15


# --------------------------------------------------------------------------- #
# Misses
# --------------------------------------------------------------------------- #


def test_a_missed_day_resets_the_streak_and_costs_points(session, user):
    log(session, user, 2, at(MONDAY, 9))
    log(session, user, 2, at(MONDAY + timedelta(days=1), 9))  # 20 points, streak 2

    # Skip Wednesday entirely; settle on Thursday morning.
    settle(session, user, at(MONDAY + timedelta(days=3), 9))

    stats = session.get(UserStats, user.id)
    assert stats.current_streak == 0
    assert stats.total_points == 5  # 20 - 15


def test_the_penalty_cannot_push_the_balance_negative(session, user):
    settle(session, user, at(MONDAY + timedelta(days=2), 9))
    stats = session.get(UserStats, user.id)
    assert stats.total_points == 0


# --------------------------------------------------------------------------- #
# Streak freeze
# --------------------------------------------------------------------------- #


def test_a_freeze_absorbs_the_miss_without_advancing_the_streak(session, user):
    stats = session.get(UserStats, user.id)
    stats.total_points = 200
    session.add(UserInventory(user_id=user.id, sku="streak_freeze", quantity=1))
    session.commit()

    log(session, user, 2, at(MONDAY, 9))  # streak 1, 210 points
    settle(session, user, at(MONDAY + timedelta(days=2), 9))  # Tuesday missed

    session.refresh(stats)
    assert stats.current_streak == 1, "freeze protects the streak"
    assert stats.total_points == 210, "no penalty charged"
    assert stats.freezes_consumed == 1

    inventory = session.get(UserInventory, (user.id, "streak_freeze"))
    assert inventory.quantity == 0


# --------------------------------------------------------------------------- #
# The seven-day week: Friday and Shabbat are ordinary days
# --------------------------------------------------------------------------- #


def test_friday_and_shabbat_are_ordinary_days_on_a_seven_day_week(session, user):
    friday = log(session, user, 2, at(FRIDAY, 10))
    saturday = log(session, user, 2, at(SATURDAY, 10))

    assert friday.units_logged == 2, "no doubled quota on a Friday"
    assert friday.day_completed and saturday.day_completed

    days = session.execute(
        select(StudyDay).where(StudyDay.user_id == user.id,
                               StudyDay.local_date.in_([FRIDAY, SATURDAY]))
    ).scalars().all()
    assert [d.required_units for d in days] == [2, 2]


def test_skipping_shabbat_on_a_seven_day_week_is_a_plain_miss(session, user):
    log(session, user, 2, at(FRIDAY, 10))
    settle(session, user, at(SATURDAY + timedelta(days=1), 9))  # Shabbat skipped

    stats = session.get(UserStats, user.id)
    assert stats.current_streak == 0
    assert statuses(session, user)[SATURDAY] == DayStatus.MISSED


# --------------------------------------------------------------------------- #
# The five-day week
# --------------------------------------------------------------------------- #


def test_the_rest_days_ask_for_nothing_and_hold_the_streak(session, five_day_user):
    user = five_day_user
    for offset in range(5):  # Sun-Thu
        log(session, user, 2, at(SUNDAY + timedelta(days=offset), 9))

    stats = session.get(UserStats, user.id)
    points_before, streak_before = stats.total_points, stats.current_streak

    # Away all weekend, back on Sunday.
    log(session, user, 2, at(SATURDAY + timedelta(days=1), 9))

    session.refresh(stats)
    assert stats.current_streak == streak_before + 1, (
        "the rest days held the streak; Sunday advanced it. A break would "
        "have restarted the count at 1."
    )
    assert stats.total_points == points_before + 15, "Sunday paid, nothing deducted"

    resolved = statuses(session, user)
    assert resolved[FRIDAY] == DayStatus.REST_DAY
    assert resolved[SATURDAY] == DayStatus.REST_DAY


def test_a_weekday_miss_still_breaks_the_streak_on_a_five_day_week(session, five_day_user):
    """The exemption is scoped to the rest days. Skipping Sunday is a miss like
    any other - otherwise the whole weekend would leak into the week."""
    user = five_day_user
    for offset in range(5):
        log(session, user, 2, at(SUNDAY + timedelta(days=offset), 9))

    settle(session, user, at(SATURDAY + timedelta(days=2), 9))  # Sunday skipped

    stats = session.get(UserStats, user.id)
    assert stats.current_streak == 0


def test_learning_on_a_rest_day_anyway_still_counts(session, five_day_user):
    """A rest day requires nothing, but it is not closed. Someone who turns up
    on Shabbat should be rewarded for it, not told the day does not count."""
    user = five_day_user
    for offset in range(5):  # Sun-Thu -> streak 5
        log(session, user, 2, at(SUNDAY + timedelta(days=offset), 9))

    result = log(session, user, 2, at(SATURDAY, 20))
    assert result.day_completed
    assert result.streak == 6, "Friday held it at 5, Shabbat advanced it"
    assert statuses(session, user)[SATURDAY] == DayStatus.COMPLETED


def test_switching_to_a_five_day_week_reclassifies_today(session, user):
    from app.services import progress

    settle(session, user, at(FRIDAY, 9))
    assert statuses(session, user)[FRIDAY] == DayStatus.PENDING

    user.study_week = StudyWeek.FIVE_DAYS
    plan = session.execute(
        select(StudyPlan).where(StudyPlan.user_id == user.id)
    ).scalar_one()
    progress.change_goal(session, user, plan, plan.daily_goal, FRIDAY)
    session.flush()

    day = session.execute(
        select(StudyDay).where(StudyDay.user_id == user.id,
                               StudyDay.local_date == FRIDAY)
    ).scalar_one()
    assert day.required_units == 0, "today stopped asking for anything"


def test_the_last_day_of_a_tractate_counts_even_when_it_is_short(session, user):
    """Berakhot is 57 mishnayot, so a goal of 2 leaves a final day of 1. That
    day cannot reach its quota, and finishing a masechta on a day that scores
    nothing is a strange way to be congratulated."""
    plan = session.execute(
        select(StudyPlan).where(StudyPlan.user_id == user.id)
    ).scalar_one()
    plan.current_ordinal = 56  # one mishnah left
    session.commit()

    result = log(session, user, 2, at(MONDAY, 9))

    assert result.units_logged == 1, "clamped to what is left in the tractate"
    assert result.plan_completed
    assert result.day_completed, "the siyum day is credited"
    assert result.points_awarded > 0
    assert statuses(session, user)[MONDAY] == DayStatus.COMPLETED


# --------------------------------------------------------------------------- #
# Idempotency - the property the whole design rests on
# --------------------------------------------------------------------------- #


def test_settling_twice_changes_nothing(session, user):
    log(session, user, 2, at(MONDAY, 9))
    moment = at(MONDAY + timedelta(days=3), 9)

    settle(session, user, moment)
    stats = session.get(UserStats, user.id)
    first = (stats.total_points, stats.current_streak, stats.last_settled_date)

    settle(session, user, moment)
    settle(session, user, moment)
    session.refresh(stats)

    assert (stats.total_points, stats.current_streak, stats.last_settled_date) == first


def test_a_replayed_log_request_does_not_double_the_cursor(session, user):
    key = "client-generated-key-1"
    study.log_study(session, user, 2, now=at(MONDAY, 9), idempotency_key=key)
    study.log_study(session, user, 2, now=at(MONDAY, 9), idempotency_key=key)

    plan = session.execute(
        select(StudyPlan).where(StudyPlan.user_id == user.id)
    ).scalar_one()
    assert plan.current_ordinal == 2


def test_two_learners_can_send_the_same_idempotency_key(session, user):
    """The key is chosen by a client that knows nothing about other accounts,
    so "today's date and how far I had got" is a key everyone picks. Matching
    on it globally answered the second learner with "already applied" and threw
    their study away."""
    other = make_user(session, StudyWeek.SEVEN_DAYS)
    key = "2026-08-10:0"

    first = study.log_study(session, user, 2, now=at(MONDAY, 9), idempotency_key=key)
    second = study.log_study(session, other, 2, now=at(MONDAY, 9), idempotency_key=key)

    assert first.units_logged == 2
    assert second.units_logged == 2, "the second learner's study must not vanish"

    plans = session.execute(select(StudyPlan)).scalars().all()
    assert [p.current_ordinal for p in plans] == [2, 2]


def test_the_ledger_always_reconciles_with_the_balance(session, user):
    from app.services.ledger import rebuild_balance

    for offset in range(3):
        log(session, user, 2, at(MONDAY + timedelta(days=offset), 9))
    settle(session, user, at(MONDAY + timedelta(days=4), 9))  # miss Thursday

    stats = session.get(UserStats, user.id)
    assert rebuild_balance(session, user.id) == stats.total_points
