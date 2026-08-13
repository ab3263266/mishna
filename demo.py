"""Print the business rules in action. No database, no server.

Everything below calls the real modules - if a rule changes, this output
changes with it. Run:

    .venv/Scripts/python.exe demo.py
"""

from __future__ import annotations

from datetime import date, timedelta

from app.services.calendar import UserClock, classify_day, required_units_for
from app.services.progress import project_completion
from app.services.scoring import ScoringRules, penalty_for_miss, points_for_completion
from app.services.zmanim import (
    CANDLE_OFFSET_BY_CITY,
    FixedClockProvider,
    Location,
    ZmanimLibraryProvider,
)

RULES = ScoringRules()
JERUSALEM = Location(
    timezone="Asia/Jerusalem",
    latitude=31.7683,
    longitude=35.2137,
    in_israel=True,
    candle_lighting_offset=CANDLE_OFFSET_BY_CITY["jerusalem"],
)
SUNDAY = date(2026, 8, 9)


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}")


def scoring_curve() -> None:
    rule("1. Scoring curve - base 10, multiplier from day 4")
    print(f"  {'day':>4} {'streak':>7} {'x':>6} {'points':>7}   running")
    total = 0
    for day in range(1, 15):
        pts = points_for_completion(RULES, day)
        total += pts
        mult = RULES.multiplier_for(day)
        flag = "  <- tier up" if day > 1 and mult != RULES.multiplier_for(day - 1) else ""
        print(f"  {day:>4} {day:>7} {float(mult):>6.1f} {pts:>7}   {total:>5}{flag}")

    print(f"\n  Miss on day 15: streak 14 -> 0, penalty -{RULES.miss_penalty}")
    print(f"  Balance {total} -> {total - penalty_for_miss(RULES, total)}")
    print(f"  Penalty is clamped: from a balance of 7 it takes only "
          f"{penalty_for_miss(RULES, 7)}, never below zero.")


def the_week() -> None:
    rule("2. A week for a Jerusalem user, daily_goal = 2")
    provider = ZmanimLibraryProvider(pre_freeze_minutes=60)
    print(f"  {'date':<12} {'day':<4} {'kind':<14} {'must log':>9}   note")

    total = 0
    for offset in range(7):
        d = SUNDAY + timedelta(days=offset)
        kind = classify_day(d, JERUSALEM, observes_shabbat=True, observes_yom_tov=True)
        req = required_units_for(kind, 2)
        total += req
        note = ""
        if kind.value == "erev_shabbat":
            note = "DOUBLE PORTION unlocks 00:00"
        elif kind.value == "shabbat":
            note = "quota already covered Friday"
        print(f"  {d.isoformat():<12} {d.strftime('%a'):<4} {kind.value:<14} {req:>9}   {note}")

    print(f"\n  Weekly total: {total} mishnayot = daily_goal x 7. "
          f"The portion is moved, not duplicated.")

    friday = SUNDAY + timedelta(days=5)
    window = provider.shabbat_window(friday, JERUSALEM)
    clock = UserClock.for_location(JERUSALEM, rollover_hour=3)
    print(f"\n  Real zmanim for {friday} (Jerusalem, {JERUSALEM.candle_lighting_offset}-min custom):")
    print(f"    penalties pause   {window.freeze_start.strftime('%a %H:%M')}")
    print(f"    candle lighting   {window.onset.strftime('%a %H:%M')}")
    print(f"    havdalah          {window.end.strftime('%a %H:%M')}   <- report window opens")
    print(f"    Motash bonus by   {clock.civil_midnight(friday + timedelta(days=1)).strftime('%a %H:%M')}")


def shabbat_paths() -> None:
    rule("3. Four ways through a Shabbat, arriving with a streak of 4")
    streak = 4
    friday = points_for_completion(RULES, streak + 1)
    shabbat = points_for_completion(RULES, streak + 2)
    pair = friday + shabbat

    rows = [
        ("logs double portion Friday morning", f"+{pair}", streak + 2, "no checkbox needed"),
        ("checks the box Motzash", f"+{pair + RULES.motash_bonus}", streak + 2, "includes Motash bonus"),
        ("checks the box Sunday", f"+{pair}", streak + 2, "bonus forfeited"),
        ("never reports", "0", streak, "streak HELD, no penalty"),
    ]
    print(f"  {'what the user does':<36} {'points':>8} {'streak':>7}   note")
    for what, pts, st, note in rows:
        print(f"  {what:<36} {pts:>8} {st:>7}   {note}")

    print(f"\n  Friday credits before Shabbat: {friday} then {shabbat} points.")
    print("  Reversing that order underpays the user at every tier boundary.")


def estimate() -> None:
    rule("4. Completion estimate - Berakhot, 57 mishnayot")
    for goal in (1, 2, 3, 5):
        p = project_completion(
            remaining_units=57,
            daily_goal=goal,
            start_date=SUNDAY,
            location=JERUSALEM,
            observes_shabbat=True,
            observes_yom_tov=True,
        )
        print(f"  {goal} per day -> finishes {p.estimated_end_date} "
              f"({p.calendar_days:>3} calendar days, {p.study_days:>3} study days)")

    slow = project_completion(
        remaining_units=57, daily_goal=2, start_date=SUNDAY, location=JERUSALEM,
        observes_shabbat=True, observes_yom_tov=True, credits_shabbat=False,
    )
    fast = project_completion(
        remaining_units=57, daily_goal=2, start_date=SUNDAY, location=JERUSALEM,
        observes_shabbat=True, observes_yom_tov=True,
    )
    print(f"\n  Skipping the double portion: {fast.estimated_end_date} -> "
          f"{slow.estimated_end_date} ({(slow.estimated_end_date - fast.estimated_end_date).days} days later)")


def fallback() -> None:
    rule("5. Fallback provider - no coordinates, no zmanim library")
    window = FixedClockProvider().shabbat_window(
        SUNDAY + timedelta(days=5), Location(timezone="Asia/Jerusalem")
    )
    print(f"  fixed clock: {window.freeze_start.strftime('%a %H:%M')} -> "
          f"{window.end.strftime('%a %H:%M')}")
    print("  Always available, deliberately approximate. Not a default for")
    print("  observant users - verify real zmanim against a published luach.")


if __name__ == "__main__":
    scoring_curve()
    the_week()
    shabbat_paths()
    estimate()
    fallback()
    print()
