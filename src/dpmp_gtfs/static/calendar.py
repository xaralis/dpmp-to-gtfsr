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

import datetime as dt
from collections.abc import Iterable, Iterator

import holidays

from dpmp_gtfs.types import CalendarException, Service

# Codes that say something about *when a trip runs*. Everything else describes
# a stop or a vehicle and is handled elsewhere.
WORKING_DAY = 2
SATURDAY = {4, 6}
SUNDAY_AND_HOLIDAYS = {5, 7}

STOP_ON_REQUEST = 1
LOW_FLOOR = 3
STEP_FREE_STOP = 8


# --- codes -> service -------------------------------------------------------


def service_from_codes(codes: Iterable[int]) -> Service:
    """Read a trip's codes as the days it runs.

    Codes 4/6 and 5/7 are exact duplicates -- per-line "fixed codes" inherited
    from JDF, used only by lines 2 and 12 -- so they are merged here rather
    than producing two services that mean the same thing.
    """
    present = set(codes)
    return Service(
        working_days=WORKING_DAY in present,
        saturday=bool(present & SATURDAY),
        sunday=bool(present & SUNDAY_AND_HOLIDAYS),
    )


# --- Czech public holidays --------------------------------------------------


def czech_holidays(year: int) -> set[dt.date]:
    """Every public holiday in a given year, fixed and movable.

    Delegated to ``holidays``, which tracks the statute (zákon č. 245/2000 Sb.)
    and computes Easter, rather than carrying a hand-written table that would
    quietly go wrong the year the law or the algorithm did. Checked against the
    previous hand-rolled implementation across 2024-2035: identical.
    """
    return set(holidays.country_holidays("CZ", years=year).keys())


def holidays_between(start: dt.date, end: dt.date) -> set[dt.date]:
    """Holidays falling within ``[start, end]``."""
    years = range(start.year, end.year + 1)
    return {d for year in years for d in czech_holidays(year) if start <= d <= end}


# --- calendar_dates.txt -----------------------------------------------------


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
