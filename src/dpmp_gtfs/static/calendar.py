"""Service calendars, derived from the API's per-trip "codes".

The upstream has no notion of a service calendar. Each trip carries a set of
small integers whose meanings come from ``/api/codes``:

===== ============================================
code  meaning
===== ============================================
1     stop on request (a property of a *stop*)
2     runs on working days
3     guaranteed low-floor vehicle
4, 6  runs on Saturday
5, 7  runs on Sunday and public holidays
8     step-free stop (a property of a *stop*)
===== ============================================

Codes 4/6 and 5/7 are exact duplicates -- they are per-line "fixed codes"
inherited from the JDF format, and only lines 2 and 12 use the second pair.
They are merged here.

Across the whole network only five distinct service patterns occur, so the
generated ``calendar.txt`` stays small and legible.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

# Codes that say something about *when a trip runs*. Everything else describes
# a stop or a vehicle and is handled elsewhere.
WORKING_DAY = 2
SATURDAY = {4, 6}
SUNDAY_AND_HOLIDAYS = {5, 7}

STOP_ON_REQUEST = 1
LOW_FLOOR = 3
STEP_FREE_STOP = 8


@dataclass(frozen=True, slots=True)
class Service:
    """A GTFS service: which weekdays a set of trips runs on."""

    working_days: bool
    saturday: bool
    sunday: bool

    @classmethod
    def from_codes(cls, codes: Iterable[int]) -> Service:
        s = set(codes)
        return cls(
            working_days=WORKING_DAY in s,
            saturday=bool(s & SATURDAY),
            sunday=bool(s & SUNDAY_AND_HOLIDAYS),
        )

    @property
    def service_id(self) -> str:
        parts = [
            name
            for flag, name in (
                (self.working_days, "wd"),
                (self.saturday, "sa"),
                (self.sunday, "su"),
            )
            if flag
        ]
        if not parts:
            raise ValueError("service runs on no days at all")
        return "-".join(parts)

    @property
    def weekday_flags(self) -> tuple[int, int, int, int, int, int, int]:
        """Monday..Sunday, as the 0/1 columns of ``calendar.txt``."""
        wd = int(self.working_days)
        return (wd, wd, wd, wd, wd, int(self.saturday), int(self.sunday))

    def runs_on(self, day: dt.date, *, holiday: bool) -> bool:
        """Whether this service operates on a given date.

        A public holiday takes the Sunday timetable regardless of which weekday
        it falls on -- that is exactly what code 5 states ("runs on Sundays and
        state-recognised holidays").
        """
        if holiday:
            return self.sunday
        weekday = day.weekday()
        if weekday == 5:
            return self.saturday
        if weekday == 6:
            return self.sunday
        return self.working_days


# --- Czech public holidays --------------------------------------------------

# Fixed-date holidays under Czech law (zákon č. 245/2000 Sb.).
_FIXED_HOLIDAYS: tuple[tuple[int, int], ...] = (
    (1, 1),  # Nový rok / Den obnovy samostatného českého státu
    (5, 1),  # Svátek práce
    (5, 8),  # Den vítězství
    (7, 5),  # Den slovanských věrozvěstů Cyrila a Metoděje
    (7, 6),  # Den upálení mistra Jana Husa
    (9, 28),  # Den české státnosti
    (10, 28),  # Den vzniku samostatného československého státu
    (11, 17),  # Den boje za svobodu a demokracii
    (12, 24),  # Štědrý den
    (12, 25),  # 1. svátek vánoční
    (12, 26),  # 2. svátek vánoční
)


def easter_sunday(year: int) -> dt.date:
    """Gregorian Easter Sunday, by the anonymous Gregorian algorithm.

    Computed rather than tabulated so the feed does not quietly go wrong in
    some future year when a hardcoded table runs out.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def czech_holidays(year: int) -> set[dt.date]:
    """Every public holiday in a given year, fixed and movable."""
    easter = easter_sunday(year)
    return {
        *(dt.date(year, month, day) for month, day in _FIXED_HOLIDAYS),
        easter - dt.timedelta(days=2),  # Velký pátek
        easter + dt.timedelta(days=1),  # Velikonoční pondělí
    }


def holidays_between(start: dt.date, end: dt.date) -> set[dt.date]:
    """Holidays falling within ``[start, end]``."""
    years = range(start.year, end.year + 1)
    return {d for year in years for d in czech_holidays(year) if start <= d <= end}


# --- calendar_dates.txt -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarException:
    service_id: str
    date: dt.date
    added: bool
    """True for GTFS exception_type 1 (added), False for 2 (removed)."""


def calendar_exceptions(
    services: Iterable[Service], start: dt.date, end: dt.date
) -> Iterator[CalendarException]:
    """Emit the ``calendar_dates.txt`` rows that make holidays behave.

    On a holiday the network runs its Sunday timetable. For every holiday that
    is *not* already a Sunday this means two corrections per service: drop the
    day it would normally have run, and add the day it now runs instead.
    """
    services = list(services)
    for date in sorted(holidays_between(start, end)):
        if date.weekday() == 6:
            continue  # already a Sunday; the regular calendar covers it
        for service in services:
            normally = service.runs_on(date, holiday=False)
            on_holiday = service.runs_on(date, holiday=True)
            if normally == on_holiday:
                continue
            yield CalendarException(service.service_id, date, added=on_holiday)
