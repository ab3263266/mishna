"""Pure business-rule tests - no database, no network, runs anywhere."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models import DayKind, StudyWeek
from app.services.calendar import UserClock, classify_day, required_units_for
from app.services.progress import project_completion
from app.services.scoring import (
    ScoringRules,
    next_tier_preview,
    penalty_for_miss,
    points_for_completion,
)

JERUSALEM = ZoneInfo("Asia/Jerusalem")
RULES = ScoringRules()

SUNDAY = date(2026, 8, 9)
FRIDAY = date(2026, 8, 14)
SATURDAY = date(2026, 8, 15)


# --------------------------------------------------------------------------- #
# Base points and the combo multiplier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "streak,expected",
    [(1, 10), (2, 10), (3, 10), (4, 15), (9, 15), (10, 20), (29, 20), (30, 25)],
)
def test_multiplier_kicks_in_on_the_fourth_day(streak: int, expected: int) -> None:
    assert points_for_completion(RULES, streak) == expected


def test_three_days_of_base_then_the_bonus() -> None:
    """'3 consecutive days -> multiplier starting on the 4th'."""
    week = [points_for_completion(RULES, n) for n in range(1, 6)]
    assert week == [10, 10, 10, 15, 15]


def test_next_tier_preview_drives_the_carrot_on_the_home_screen() -> None:
    assert next_tier_preview(RULES, 2) == (2, RULES.multiplier_for(4))
    assert next_tier_preview(RULES, 30) is None


# --------------------------------------------------------------------------- #
# The penalty
# --------------------------------------------------------------------------- #


def test_penalty_is_capped_by_the_balance() -> None:
    assert penalty_for_miss(RULES, current_balance=100) == 15
    assert penalty_for_miss(RULES, current_balance=7) == 7
    assert penalty_for_miss(RULES, current_balance=0) == 0


# --------------------------------------------------------------------------- #
# Day boundaries
# --------------------------------------------------------------------------- #


def test_late_night_study_counts_for_the_day_that_just_ended() -> None:
    clock = UserClock(tz=JERUSALEM, rollover_hour=3)
    late = datetime(2026, 8, 12, 1, 30, tzinfo=JERUSALEM)
    assert clock.local_date(late) == date(2026, 8, 11)

    morning = datetime(2026, 8, 12, 7, 0, tzinfo=JERUSALEM)
    assert clock.local_date(morning) == date(2026, 8, 12)


def test_local_date_is_the_users_date_not_utc() -> None:
    clock = UserClock(tz=ZoneInfo("America/New_York"), rollover_hour=3)
    # 03:00 UTC Wednesday is still 23:00 Tuesday in New York.
    moment = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
    assert clock.local_date(moment) == date(2026, 8, 11)


# --------------------------------------------------------------------------- #
# The week mode
# --------------------------------------------------------------------------- #


def test_a_seven_day_week_treats_every_day_alike() -> None:
    """Including Friday and Shabbat: no doubled quota, no exemption."""
    for offset in range(7):
        day = SUNDAY + timedelta(days=offset)
        assert classify_day(day, StudyWeek.SEVEN_DAYS) == DayKind.WEEKDAY
        assert required_units_for(DayKind.WEEKDAY, 2) == 2


def test_a_five_day_week_rests_on_friday_and_shabbat() -> None:
    assert classify_day(FRIDAY, StudyWeek.FIVE_DAYS) == DayKind.REST_DAY
    assert classify_day(SATURDAY, StudyWeek.FIVE_DAYS) == DayKind.REST_DAY
    assert classify_day(SUNDAY, StudyWeek.FIVE_DAYS) == DayKind.WEEKDAY
    assert required_units_for(DayKind.REST_DAY, 2) == 0


def test_the_shorter_week_learns_less_and_does_not_make_it_up() -> None:
    """Nothing is carried onto a neighbouring day: five days a week means five
    days' worth of mishnayot, which is the whole point of choosing it."""
    goal = 2
    week = [
        required_units_for(classify_day(SUNDAY + timedelta(days=d), mode), goal)
        for d in range(7)
        for mode in [StudyWeek.FIVE_DAYS]
    ]
    assert sum(week) == goal * 5


# --------------------------------------------------------------------------- #
# The completion estimate
# --------------------------------------------------------------------------- #


def test_seven_day_estimate_is_a_plain_division() -> None:
    projection = project_completion(
        remaining_units=20,
        daily_goal=2,
        start_date=SUNDAY,
        study_week=StudyWeek.SEVEN_DAYS,
    )
    assert projection.calendar_days == 10
    assert projection.study_days == 10
    assert projection.units_per_week == 14


def test_five_day_estimate_walks_past_the_rest_days() -> None:
    """10 units at 2/day from a Sunday is Sun-Thu - and lands on Thursday, not
    on the Friday a naive `remaining / goal` would count."""
    projection = project_completion(
        remaining_units=10,
        daily_goal=2,
        start_date=SUNDAY,
        study_week=StudyWeek.FIVE_DAYS,
    )
    assert projection.estimated_end_date == date(2026, 8, 13)  # Thursday
    assert projection.study_days == 5
    assert projection.units_per_week == 10


def test_the_shorter_week_finishes_later() -> None:
    seven = project_completion(
        remaining_units=100, daily_goal=2, start_date=SUNDAY,
        study_week=StudyWeek.SEVEN_DAYS,
    )
    five = project_completion(
        remaining_units=100, daily_goal=2, start_date=SUNDAY,
        study_week=StudyWeek.FIVE_DAYS,
    )
    assert five.estimated_end_date > seven.estimated_end_date
    assert five.study_days == seven.study_days  # same work, spread wider


def test_a_plan_starting_on_a_rest_day_starts_counting_on_sunday() -> None:
    projection = project_completion(
        remaining_units=2,
        daily_goal=2,
        start_date=FRIDAY,
        study_week=StudyWeek.FIVE_DAYS,
    )
    assert projection.estimated_end_date == date(2026, 8, 16)  # the Sunday
    assert projection.study_days == 1
