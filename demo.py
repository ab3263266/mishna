"""Print the business rules in action. No database, no server.

Everything below calls the real modules - if a rule changes, this output
changes with it. Run:

    .venv/Scripts/python.exe demo.py
"""

from __future__ import annotations

from datetime import date, timedelta

from app.models import StudyWeek
from app.services.calendar import classify_day, required_units_for
from app.services.progress import project_completion
from app.services.scoring import ScoringRules, penalty_for_miss, points_for_completion
from app.services.texts import COMMENTATORS, available_tractates, get_mishnah

RULES = ScoringRules()
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
    rule("2. The two week modes, daily_goal = 2")
    print(f"  {'date':<12} {'day':<4} {'7-day':>7} {'5-day':>7}   note")

    totals = {StudyWeek.SEVEN_DAYS: 0, StudyWeek.FIVE_DAYS: 0}
    for offset in range(7):
        d = SUNDAY + timedelta(days=offset)
        required = {}
        for mode in totals:
            required[mode] = required_units_for(classify_day(d, mode), 2)
            totals[mode] += required[mode]
        note = "rest day on the 5-day plan" if not required[StudyWeek.FIVE_DAYS] else ""
        print(f"  {d.isoformat():<12} {d.strftime('%a'):<4} "
              f"{required[StudyWeek.SEVEN_DAYS]:>7} {required[StudyWeek.FIVE_DAYS]:>7}   {note}")

    print(f"\n  Weekly totals: {totals[StudyWeek.SEVEN_DAYS]} vs "
          f"{totals[StudyWeek.FIVE_DAYS]} mishnayot.")
    print("  Nothing is carried over: the shorter week learns less, it does not")
    print("  cram the same quota into five days.")


def rest_days() -> None:
    rule("3. A rest day, arriving with a streak of 4")
    rows = [
        ("does not open the app", "0", 4, "streak HELD, no penalty"),
        ("reads the portion anyway",
         f"+{points_for_completion(RULES, 5)}", 5, "credited like any day"),
    ]
    print(f"  {'what the user does':<28} {'points':>8} {'streak':>7}   note")
    for what, pts, streak, note in rows:
        print(f"  {what:<28} {pts:>8} {streak:>7}   {note}")
    print("\n  A rest day is never punished, and never blocked either.")


def estimate() -> None:
    rule("4. Completion estimate - Berakhot, 57 mishnayot")
    for goal in (1, 2, 3, 5):
        row = []
        for mode in (StudyWeek.SEVEN_DAYS, StudyWeek.FIVE_DAYS):
            p = project_completion(
                remaining_units=57, daily_goal=goal, start_date=SUNDAY,
                study_week=mode,
            )
            row.append(f"{p.estimated_end_date} ({p.calendar_days:>3}d)")
        print(f"  {goal} per day -> 7-day week {row[0]}   5-day week {row[1]}")


def the_text() -> None:
    rule("5. The text - read from disk, never from the network")
    print(f"  {len(available_tractates())} tractates on disk, "
          f"{len(COMMENTATORS)} commentaries each")

    class _T:
        slug, name_he = "berakhot", "ברכות"

    view = get_mishnah(_T(), 1)
    print(f"\n  {view.ref} ({view.text.license}, {view.text.version_title})")
    print(f"    {view.text.body[:64]}...")
    for passage in view.commentaries:
        print(f"    {passage.title:<24} {len(passage.body):>6} chars  {passage.license}")


if __name__ == "__main__":
    scoring_curve()
    the_week()
    rest_days()
    estimate()
    the_text()
    print()
