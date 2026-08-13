"""Shabbat / Yom Tov windows.

The whole "Sabbath Mode" feature reduces to one question the rest of the code
can ask: *is this user currently inside a rest window, and which local dates
does it cover?* Everything else - deferred penalties, the double portion, the
Motzash checkbox - is written in terms of that answer.

Two providers:

* `ZmanimLibraryProvider` - real sunset-based times from the `zmanim` package
  (a port of KosherJava), which is what you want in production. Candle lighting
  is sunset minus an offset; havdalah is tzeis.
* `FixedClockProvider` - fixed local wall-clock fallback (Fri 18:00 -> Sat
  20:00). Used when the library is missing or the user never shared a location.
  Deliberately *wider* than reality in winter and narrower in summer, so treat
  it as a stopgap, not a default for observant users.

IMPORTANT: verify the library provider's output against a published luach for a
few dates in your target cities before launch. Being 20 minutes late with a
"freeze" is a support ticket; being 20 minutes early is a halachic complaint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

FRIDAY = 4  # date.weekday()
SATURDAY = 5


#: Minutes before sunset at which candles are lit. 18 is the common default,
#: but it is a *local custom*, not a constant - and getting it wrong shifts the
#: whole freeze window. Jerusalem is the one that bites: 40 minutes, which is
#: 22 minutes earlier than the default would give you.
DEFAULT_CANDLE_OFFSET = 18
CANDLE_OFFSET_BY_CITY = {
    "jerusalem": 40,
    "haifa": 30,
    "zefat": 30,
    "petah_tikva": 20,
    "beer_sheva": 22,
}


@dataclass(frozen=True, slots=True)
class Location:
    timezone: str
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float = 0.0
    in_israel: bool = True
    #: Per-user, because two users in the same timezone can follow different
    #: customs. Seed it from CANDLE_OFFSET_BY_CITY during onboarding and let
    #: the user override it in settings.
    candle_lighting_offset: int = DEFAULT_CANDLE_OFFSET

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True, slots=True)
class RestWindow:
    """A period during which the user is off-device.

    `freeze_start` is earlier than `onset`: penalties stop accruing on Friday
    *afternoon* (spec 3.2), before candle lighting, so a user who closes the
    laptop early to prepare is never punished for it.
    """

    kind: str  # "shabbat" | "yom_tov"
    label: str
    freeze_start: datetime  # UTC - penalties pause here
    onset: datetime  # UTC - candle lighting
    end: datetime  # UTC - havdalah / tzeis
    covered_dates: tuple[date, ...]  # local dates that get rest-day treatment

    def contains(self, moment: datetime) -> bool:
        return self.freeze_start <= moment < self.end


class ZmanimProvider(Protocol):
    def shabbat_window(self, friday: date, loc: Location) -> RestWindow: ...


class FixedClockProvider:
    """No astronomy: fixed local times. Safe, boring, always available."""

    def __init__(
        self,
        onset_hour: int = 18,
        end_hour: int = 20,
        pre_freeze_minutes: int = 60,
    ) -> None:
        self.onset_hour = onset_hour
        self.end_hour = end_hour
        self.pre_freeze_minutes = pre_freeze_minutes

    def shabbat_window(self, friday: date, loc: Location) -> RestWindow:
        if friday.weekday() != FRIDAY:
            raise ValueError(f"{friday} is not a Friday")
        tz = loc.tz
        saturday = friday + timedelta(days=1)
        onset = datetime.combine(friday, time(self.onset_hour), tzinfo=tz)
        end = datetime.combine(saturday, time(self.end_hour), tzinfo=tz)
        return RestWindow(
            kind="shabbat",
            label="שבת",
            freeze_start=onset - timedelta(minutes=self.pre_freeze_minutes),
            onset=onset,
            end=end,
            covered_dates=(friday, saturday),
        )


class ZmanimLibraryProvider:
    """Sunset-based times via the `zmanim` package."""

    def __init__(
        self,
        pre_freeze_minutes: int = 60,
        fallback: ZmanimProvider | None = None,
    ) -> None:
        self.pre_freeze_minutes = pre_freeze_minutes
        self.fallback = fallback or FixedClockProvider(
            pre_freeze_minutes=pre_freeze_minutes
        )

    def shabbat_window(self, friday: date, loc: Location) -> RestWindow:
        if friday.weekday() != FRIDAY:
            raise ValueError(f"{friday} is not a Friday")
        if not loc.has_coordinates:
            return self.fallback.shabbat_window(friday, loc)

        try:
            from zmanim.util.geo_location import GeoLocation
            from zmanim.zmanim_calendar import ZmanimCalendar
        except ImportError:  # pragma: no cover - depends on the environment
            logger.warning("zmanim not installed; using fixed-clock Shabbat times")
            return self.fallback.shabbat_window(friday, loc)

        saturday = friday + timedelta(days=1)
        geo = GeoLocation(
            "user", loc.latitude, loc.longitude, loc.timezone, elevation=loc.elevation_m
        )
        erev = ZmanimCalendar(
            # The offset follows the user's local custom, not a global default.
            candle_lighting_offset=loc.candle_lighting_offset,
            geo_location=geo,
            date=friday,
        )
        motzash = ZmanimCalendar(geo_location=geo, date=saturday)

        onset = erev.candle_lighting()
        end = motzash.tzais()
        if onset is None or end is None:
            # Polar latitudes: no sunset that day. Fall back rather than guess.
            logger.warning("no sunset for %s at %s; using fixed clock", friday, loc)
            return self.fallback.shabbat_window(friday, loc)

        return RestWindow(
            kind="shabbat",
            label="שבת",
            freeze_start=onset - timedelta(minutes=self.pre_freeze_minutes),
            onset=onset,
            end=end,
            covered_dates=(friday, saturday),
        )


def is_yom_tov(d: date, in_israel: bool = True) -> bool:
    """Device-prohibited festival days (not Chol HaMoed).

    Uses pyluach when available. Returns False if unavailable - which degrades
    to "Yom Tov is treated as a normal weekday", the conservative direction:
    users can still cover it with a Streak Freeze rather than being silently
    credited for days they did not learn.
    """
    try:
        from pyluach import dates as h_dates
    except ImportError:  # pragma: no cover
        return False

    festival = h_dates.HebrewDate.from_pydate(d).festival(
        israel=in_israel, include_working_days=False
    )
    return festival is not None


def window_for_moment(
    provider: ZmanimProvider, loc: Location, moment: datetime
) -> RestWindow | None:
    """Return the rest window containing `moment`, if any.

    Checks this week's Friday and the previous one, because at 01:00 Saturday
    the relevant window started on a date that is no longer "today".
    """
    local_date = moment.astimezone(loc.tz).date()
    for delta in range(0, 8):
        candidate = local_date - timedelta(days=delta)
        if candidate.weekday() != FRIDAY:
            continue
        window = provider.shabbat_window(candidate, loc)
        return window if window.contains(moment) else None
    return None


def enclosing_shabbat_friday(d: date) -> date:
    """The Friday of the Shabbat that `d` belongs to."""
    if d.weekday() == FRIDAY:
        return d
    if d.weekday() == SATURDAY:
        return d - timedelta(days=1)
    raise ValueError(f"{d} is neither Friday nor Saturday")
