"""Pure business-rule tests - no database, no network, runs anywhere.

Every assertion here maps to a numbered line in the spec.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models import DayKind
from app.services.calendar import (
    UserClock,
    classify_day,
    required_units_for,
    shabbat_report_deadline,
)
from app.services.progress import project_completion
from app.services.scoring import (
    ScoringRules,
    next_tier_preview,
    penalty_for_miss,
    points_for_completion,
)
from app.services.zmanim import FixedClockProvider, Location, window_for_moment

JERUSALEM = ZoneInfo("Asia/Jerusalem")
LOC = Location(timezone="Asia/Jerusalem", in_israel=True)
RULES = ScoringRules()

# 2026-08-14 is a Friday, 2026-08-15 a Saturday.
FRIDAY = date(2026, 8, 14)
SATURDAY = date(2026, 8, 15)


# --------------------------------------------------------------------------- #
# Spec 2.1 / 2.2 - base points and the combo multiplier
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
# Spec 2.3 - the penalty
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


def test_motash_bonus_uses_real_midnight_not_the_rollover() -> None:
    clock = UserClock(tz=JERUSALEM, rollover_hour=3)
    assert clock.civil_midnight(SATURDAY) == datetime(
        2026, 8, 16, 0, 0, tzinfo=JERUSALEM
    )


# --------------------------------------------------------------------------- #
# Spec 3.1 - the double portion
# --------------------------------------------------------------------------- #


def test_friday_carries_a_double_portion_and_shabbat_carries_none() -> None:
    assert classify_day(FRIDAY, LOC, True, False) == DayKind.EREV_SHABBAT
    assert classify_day(SATURDAY, LOC, True, False) == DayKind.SHABBAT

    assert required_units_for(DayKind.EREV_SHABBAT, 2) == 4
    assert required_units_for(DayKind.SHABBAT, 2) == 0
    assert required_units_for(DayKind.WEEKDAY, 2) == 2


def test_the_weekly_quota_is_unchanged_by_shabbat_mode() -> None:
    """The portion is moved, not duplicated: still goal x 7 per week."""
    goal = 2
    week = [
        required_units_for(classify_day(FRIDAY + timedelta(days=d), LOC, True, False), goal)
        for d in range(7)
    ]
    assert sum(week) == goal * 7


def test_a_user_who_does_not_keep_shabbat_gets_plain_weekdays() -> None:
    assert classify_day(SATURDAY, LOC, observes_shabbat=False, observes_yom_tov=False) == (
        DayKind.WEEKDAY
    )


# --------------------------------------------------------------------------- #
# Spec 3.2 - the freeze window
# --------------------------------------------------------------------------- #


def test_penalties_pause_from_friday_afternoon_until_motzash() -> None:
    provider = FixedClockProvider(onset_hour=18, end_hour=20, pre_freeze_minutes=60)
    window = provider.shabbat_window(FRIDAY, LOC)

    assert window.freeze_start == datetime(2026, 8, 14, 17, 0, tzinfo=JERUSALEM)
    assert window.end == datetime(2026, 8, 15, 20, 0, tzinfo=JERUSALEM)
    assert window.covered_dates == (FRIDAY, SATURDAY)

    inside = datetime(2026, 8, 14, 17, 30, tzinfo=JERUSALEM)
    before = datetime(2026, 8, 14, 12, 0, tzinfo=JERUSALEM)
    after = datetime(2026, 8, 15, 21, 0, tzinfo=JERUSALEM)

    assert window.contains(inside)
    assert not window.contains(before)
    assert not window.contains(after)


def test_saturday_small_hours_still_resolve_to_fridays_window() -> None:
    """01:00 Saturday belongs to a window that opened on a date which is no
    longer 'today' - the lookback is what makes this work."""
    provider = FixedClockProvider()
    moment = datetime(2026, 8, 15, 1, 0, tzinfo=JERUSALEM)
    assert window_for_moment(provider, LOC, moment) is not None


def test_wednesday_is_not_in_a_window() -> None:
    provider = FixedClockProvider()
    moment = datetime(2026, 8, 12, 14, 0, tzinfo=JERUSALEM)
    assert window_for_moment(provider, LOC, moment) is None


def test_candle_lighting_offset_follows_local_custom() -> None:
    """Jerusalem lights 40 minutes before sunset, not the usual 18. Hard-coding
    18 shifts the whole freeze window 22 minutes late - which in winter means
    the app is still live when the user has already put the phone down."""
    pytest.importorskip("zmanim")
    from app.services.zmanim import ZmanimLibraryProvider

    provider = ZmanimLibraryProvider(pre_freeze_minutes=60)
    coords = {"latitude": 31.7683, "longitude": 35.2137}
    winter = date(2026, 12, 18)

    default = provider.shabbat_window(
        winter, Location(timezone="Asia/Jerusalem", **coords)
    )
    jerusalem = provider.shabbat_window(
        winter,
        Location(timezone="Asia/Jerusalem", candle_lighting_offset=40, **coords),
    )

    assert (default.onset - jerusalem.onset) == timedelta(minutes=22)
    # The freeze window shifts with it - that is the point.
    assert (default.freeze_start - jerusalem.freeze_start) == timedelta(minutes=22)


# --------------------------------------------------------------------------- #
# Spec 3.4 - the report window
# --------------------------------------------------------------------------- #


def test_report_window_closes_at_the_end_of_sundays_study_day() -> None:
    clock = UserClock(tz=JERUSALEM, rollover_hour=3)
    deadline = shabbat_report_deadline(clock, SATURDAY, grace_days=1)
    # End of Sunday's study day == Monday 03:00 local.
    assert deadline == datetime(2026, 8, 17, 3, 0, tzinfo=JERUSALEM)


# --------------------------------------------------------------------------- #
# Spec 1.3 - the completion estimate
# --------------------------------------------------------------------------- #


def test_estimate_accounts_for_the_double_portion() -> None:
    """14 units at 2/day over a Shabbat-inclusive week = exactly 7 days."""
    projection = project_completion(
        remaining_units=14,
        daily_goal=2,
        start_date=date(2026, 8, 9),  # a Sunday
        location=LOC,
        observes_shabbat=True,
        observes_yom_tov=False,
    )
    assert projection.estimated_end_date == date(2026, 8, 15)
    assert projection.calendar_days == 7
    # Six study days, because Shabbat requires no logging.
    assert projection.study_days == 6


def test_finishing_on_fridays_own_half_ends_on_friday() -> None:
    """12 units: Sun-Thu covers 10, Friday's own 2 finishes it. No Shabbat
    units are consumed, so the estimate must not slide to Saturday."""
    projection = project_completion(
        remaining_units=12,
        daily_goal=2,
        start_date=date(2026, 8, 9),
        location=LOC,
        observes_shabbat=True,
        observes_yom_tov=False,
    )
    assert projection.estimated_end_date == FRIDAY


def test_skipping_shabbat_study_stretches_the_estimate() -> None:
    with_shabbat = project_completion(
        remaining_units=100,
        daily_goal=2,
        start_date=date(2026, 8, 9),
        location=LOC,
        observes_shabbat=True,
        observes_yom_tov=False,
    )
    without = project_completion(
        remaining_units=100,
        daily_goal=2,
        start_date=date(2026, 8, 9),
        location=LOC,
        observes_shabbat=True,
        observes_yom_tov=False,
        credits_shabbat=False,
    )
    assert without.estimated_end_date > with_shabbat.estimated_end_date


def test_a_non_observant_users_estimate_is_a_plain_division() -> None:
    projection = project_completion(
        remaining_units=20,
        daily_goal=2,
        start_date=date(2026, 8, 9),
        location=LOC,
        observes_shabbat=False,
        observes_yom_tov=False,
    )
    assert projection.calendar_days == 10
