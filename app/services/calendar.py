"""Local-day arithmetic.

A "day" in this product is not a UTC day and not even a civil day: study logged
at 01:30 belongs to the evening that just ended. `UserClock` is the single
place that decision is made, so no other module ever calls `.date()` on a
timestamp directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import DayKind, User
from app.services.zmanim import FRIDAY, SATURDAY, Location, is_yom_tov


def location_for(user: User) -> Location:
    """The single place a User becomes a Location.

    Kept in one function so a new field (candle offset, elevation, custom
    havdalah) cannot be wired into two of the three call sites and silently
    diverge.
    """
    return Location(
        timezone=user.timezone,
        latitude=user.latitude,
        longitude=user.longitude,
        elevation_m=user.elevation_m,
        in_israel=user.in_israel,
        candle_lighting_offset=user.candle_lighting_offset,
    )


@dataclass(frozen=True, slots=True)
class UserClock:
    tz: ZoneInfo
    rollover_hour: int = 3

    @classmethod
    def for_location(cls, loc: Location, rollover_hour: int = 3) -> UserClock:
        return cls(tz=loc.tz, rollover_hour=rollover_hour)

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

    def civil_midnight(self, d: date) -> datetime:
        """True local 00:00 of the day *after* `d`.

        The Motash bonus is specified as "before midnight on Saturday", which
        means real midnight - not our 03:00 rollover.
        """
        return datetime.combine(d + timedelta(days=1), time(0), tzinfo=self.tz)


def classify_day(d: date, loc: Location, observes_shabbat: bool,
                 observes_yom_tov: bool) -> DayKind:
    """What kind of day this is for this user."""
    if observes_yom_tov and is_yom_tov(d, loc.in_israel):
        return DayKind.YOM_TOV
    if not observes_shabbat:
        return DayKind.WEEKDAY
    if d.weekday() == FRIDAY:
        return DayKind.EREV_SHABBAT
    if d.weekday() == SATURDAY:
        return DayKind.SHABBAT
    return DayKind.WEEKDAY


def required_units_for(kind: DayKind, daily_goal: int) -> int:
    """Spec 3.1: Friday carries a Double Portion, Shabbat carries none.

    The quota is *moved*, not duplicated - over the week the user still learns
    daily_goal x 7. Shabbat's row exists (so it can hold points and a streak
    snapshot) but requires no in-app logging.
    """
    match kind:
        case DayKind.EREV_SHABBAT:
            return daily_goal * 2
        case DayKind.SHABBAT | DayKind.YOM_TOV:
            return 0
        case _:
            return daily_goal


def is_rest_day(kind: DayKind) -> bool:
    return kind in (DayKind.SHABBAT, DayKind.YOM_TOV)


def shabbat_report_deadline(clock: UserClock, saturday: date, grace_days: int) -> datetime:
    """Until when the Motzash checkbox stays open.

    Default grace of 1 day means: reportable until the end of Sunday's study
    day. Miss that and Friday/Saturday lock as SHABBAT_UNREPORTED - neutral, not
    punished.
    """
    return clock.day_end(saturday + timedelta(days=grace_days))
