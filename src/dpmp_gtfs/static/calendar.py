"""Service calendars, derived from the API's per-trip "codes".

The upstream has no notion of a service calendar: each trip just carries a set
of small integers. What those integers mean -- including the two pairs that are
duplicates of each other -- is recorded in :mod:`dpmp_gtfs.upstream`.

Across the whole network only five distinct service patterns occur, so the
generated ``calendar.txt`` stays small and legible.
"""

import datetime as dt
from collections.abc import Iterable, Iterator

import holidays

from dpmp_gtfs.types import CalendarException, Service
from dpmp_gtfs.upstream import SATURDAY, SUNDAY_AND_HOLIDAYS, WORKING_DAY

# --- codes -> service -------------------------------------------------------


def service_from_codes(codes: Iterable[int]) -> Service:
    """Read a trip's codes as the days it runs.

    The duplicate code pairs are merged by construction: ``SATURDAY`` and
    ``SUNDAY_AND_HOLIDAYS`` are sets, so both spellings land on one service
    rather than splitting identical trips across two calendars.
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
