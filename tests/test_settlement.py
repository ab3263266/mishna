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
from datetime import UTC, date, datetime, timedelta
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
    Tractate,
    User,
    UserInventory,
    UserStats,
)
from app.services import settlement, shop, study
from app.services.zmanim import FixedClockProvider

TEST_DB = os.getenv("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")

JERUSALEM = ZoneInfo("Asia/Jerusalem")
PROVIDER = FixedClockProvider(onset_hour=18, end_hour=20, pre_freeze_minutes=60)

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


@pytest.fixture
def user(session: Session) -> User:
    u = User(
        google_sub=f"sub-{uuid.uuid4()}",
        email="a@example.com",
        timezone="Asia/Jerusalem",
        observes_shabbat=True,
        observes_yom_tov=False,
    )
    session.add(u)
    session.flush()
    session.add(UserStats(user_id=u.id, total_points=0))
    session.add(
        StudyPlan(user_id=u.id, tractate_id=1, daily_goal=2, start_date=MONDAY)
    )
    session.commit()
    return u


def settle(session, user, moment):
    return settlement.settle_user(session, user, moment, provider=PROVIDER)


def log(session, user, units, moment):
    return study.log_study(session, user, units, now=moment)


# --------------------------------------------------------------------------- #
# Spec 2.1 / 2.2
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
# Spec 2.3
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
# Spec 2.4 - streak freeze
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
# Spec 3.3 - no login over Shabbat is free
# --------------------------------------------------------------------------- #


def test_shabbat_without_a_report_costs_nothing(session, user):
    for offset in range(4):  # Mon-Thu -> streak 4
        log(session, user, 2, at(MONDAY + timedelta(days=offset), 9))
    stats = session.get(UserStats, user.id)
    points_before, streak_before = stats.total_points, stats.current_streak

    # Off-device all Shabbat, never reports, resumes Sunday as normal.
    log(session, user, 2, at(SATURDAY + timedelta(days=1), 9))
    # Monday: the report window has closed, so Fri/Sat finalise.
    settle(session, user, at(SATURDAY + timedelta(days=2), 9))

    session.refresh(stats)
    assert stats.current_streak == streak_before + 1, (
        "Shabbat held the streak; Sunday advanced it. A break would have "
        "restarted the count at 1."
    )
    assert stats.total_points == points_before + 15, "Sunday paid, nothing deducted"

    statuses = {
        d.local_date: d.status
        for d in session.execute(
            select(StudyDay).where(StudyDay.user_id == user.id)
        ).scalars()
    }
    assert statuses[FRIDAY] == DayStatus.SHABBAT_UNREPORTED
    assert statuses[SATURDAY] == DayStatus.SHABBAT_UNREPORTED


def test_a_plain_weekday_miss_still_breaks_the_streak_after_shabbat(session, user):
    """The Shabbat exemption is scoped to Shabbat. Skipping Sunday is a miss
    like any other - otherwise the freeze would leak into the whole weekend."""
    for offset in range(4):
        log(session, user, 2, at(MONDAY + timedelta(days=offset), 9))

    settle(session, user, at(SATURDAY + timedelta(days=2), 9))  # Sunday skipped

    stats = session.get(UserStats, user.id)
    assert stats.current_streak == 0


# --------------------------------------------------------------------------- #
# Spec 3.2 - the penalty timer pauses
# --------------------------------------------------------------------------- #


def test_thursdays_miss_is_not_judged_during_the_shabbat_window(session, user):
    log(session, user, 2, at(MONDAY, 9))
    # Thursday goes unfinished. Settle at Friday 18:30, inside the window.
    outcome = settle(session, user, at(FRIDAY, 18, 30))

    stats = session.get(UserStats, user.id)
    assert stats.total_points == 10, "no penalty charged inside the window"
    assert outcome.stopped_reason and "deferred" in outcome.stopped_reason


# --------------------------------------------------------------------------- #
# Spec 3.5 / 3.6 / 3.7 - the Motzash report
# --------------------------------------------------------------------------- #


def test_motzash_report_credits_both_days_in_order(session, user):
    for offset in range(4):  # Mon-Thu -> streak 4
        log(session, user, 2, at(MONDAY + timedelta(days=offset), 9))
    points_before = session.get(UserStats, user.id).total_points  # 45

    result = study.report_shabbat(session, user, now=at(SATURDAY, 21, 0))

    # Streak was 4, so Friday scores as day 5 and Shabbat as day 6 - both at
    # the 1.5x tier. Order matters: crediting Shabbat first would pay the same
    # here but not at a tier boundary.
    assert result.friday_points == 15
    assert result.shabbat_points == 15
    assert result.motash_bonus == 5, "reported before local midnight"
    assert result.streak == 6

    stats = session.get(UserStats, user.id)
    assert stats.total_points == points_before + 35


def test_reporting_after_midnight_forfeits_only_the_bonus(session, user):
    log(session, user, 2, at(MONDAY, 9))
    result = study.report_shabbat(session, user, now=at(SATURDAY + timedelta(days=1), 10))

    assert result.motash_bonus == 0
    assert result.friday_points > 0 and result.shabbat_points > 0


def test_the_report_advances_the_reading_cursor_by_the_double_portion(session, user):
    plan = session.execute(
        select(StudyPlan).where(StudyPlan.user_id == user.id)
    ).scalar_one()
    before = plan.current_ordinal

    result = study.report_shabbat(session, user, now=at(SATURDAY, 21))
    session.refresh(plan)

    assert result.units_credited == 4  # 2 x daily_goal
    assert plan.current_ordinal == before + 4


def test_logging_the_double_portion_on_friday_needs_no_checkbox(session, user):
    result = log(session, user, 4, at(FRIDAY, 10))
    assert result.day_completed, "4 units meets Friday's doubled requirement"
    streak_after_friday = result.streak

    # Settle on Sunday - Shabbat credits off the logged double portion without
    # waiting for the report deadline, because the work is already recorded.
    settle(session, user, at(SATURDAY + timedelta(days=1), 9))

    stats = session.get(UserStats, user.id)
    assert stats.current_streak == streak_after_friday + 1, "Shabbat credited too"


def test_reporting_before_havdalah_is_rejected(session, user):
    with pytest.raises(study.StudyError) as exc:
        study.report_shabbat(session, user, now=at(SATURDAY, 15))
    assert exc.value.code == "shabbat_not_over"


def test_the_report_window_closes_after_the_grace_period(session, user):
    with pytest.raises(study.StudyError) as exc:
        study.report_shabbat(session, user, now=at(SATURDAY + timedelta(days=3), 10))
    assert exc.value.code == "report_window_closed"


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


def test_the_ledger_always_reconciles_with_the_balance(session, user):
    from app.services.ledger import rebuild_balance

    for offset in range(3):
        log(session, user, 2, at(MONDAY + timedelta(days=offset), 9))
    settle(session, user, at(MONDAY + timedelta(days=4), 9))  # miss Thursday

    stats = session.get(UserStats, user.id)
    assert rebuild_balance(session, user.id) == stats.total_points
