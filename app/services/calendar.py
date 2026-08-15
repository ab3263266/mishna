"""Local-day arithmetic.

A "day" in this product is not a UTC day and not even a civil day: study logged
at 01:30 belongs to the evening that just ended. `UserClock` is the single
place that decision is made, so no other module ever calls `.date()` on a
timestamp directly.

The only calendar rule beyond that is the week mode: a five-day learner rests
on Friday and Shabbat, a seven-day learner never rests. There is deliberately
no sunset arithmetic here - a day is a plain local date, and which dates carry
a quota is a lookup on `date.weekday()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import DayKind, StudyWeek, User

FRIDAY = 4  # date.weekday()
SATURDAY = 5

#: The days a five-day week leaves out. Friday and Shabbat, because the working
#: week this app is built around runs Sunday to Thursday.
REST_WEEKDAYS = frozenset({FRIDAY, SATURDAY})


@dataclass(frozen=True, slots=True)
class UserClock:
    tz: ZoneInfo
    rollover_hour: int = 3

    @classmethod
    def for_user(cls, user: User, rollover_hour: int = 3) -> UserClock:
        return cls(tz=ZoneInfo(user.timezone), rollover_hour=rollover_hour)

    def local_date(self, moment: datetime) -> date:
        """The study-day a UTC instant belongs to."""
        local = moment.astimezone(self.tz)
        return (local - timedelta(hours=self.rollover_hour)).date()

    def day_start(self, d: date) -> datetime:
        """UTC instant at which study-day `d` opens."""
        naive = datetime.combine(d, time(self.rollover_hour))
        return naive.replace(tzinfo=self.tz)

    def day_end(self, d: date) -> datetime:
        return self.day_start(d + timedelta(days=1))


def classify_day(d: date, study_week: StudyWeek) -> DayKind:
    """Whether this date asks anything of this learner."""
    if study_week == StudyWeek.FIVE_DAYS and d.weekday() in REST_WEEKDAYS:
        return DayKind.REST_DAY
    return DayKind.WEEKDAY


def required_units_for(kind: DayKind, daily_goal: int) -> int:
    """A rest day requires nothing.

    Nothing is carried over to a neighbouring day: five days a week means five
    days' worth of mishnayot, not seven days' worth crammed into five. That is
    the whole point of choosing the shorter week.
    """
    return 0 if kind is DayKind.REST_DAY else daily_goal


def is_rest_day(kind: DayKind) -> bool:
    return kind is DayKind.REST_DAY
