"""Service calendars, derived from each trip's JDF fixed codes.

The upstream has no notion of a service calendar: a trip carries a set of
one-character codes inherited from JDF. The old API published their meanings
at ``/api/codes``; the new one does not, so the mapping is spelled out here
against the JDF convention.

Case is significant. Upper-case ``X`` on a trip means "runs on weekdays";
lower-case ``x`` is a *stop*-level marker meaning "request stop" and must
never be read as a calendar code.

Across the whole network only a handful of distinct service patterns occur, so
the generated ``calendar.txt`` stays small and legible.
"""

import datetime as dt
from collections.abc import Iterable, Iterator

import holidays

from dpmp_gtfs.api.models import SATURDAY, SUNDAY_AND_HOLIDAYS, WORKING_DAYS
from dpmp_gtfs.types import CalendarException, Service


def service_from_codes(codes: Iterable[str]) -> Service:
    """Read a trip's fixed codes as the days it runs.

    Codes that describe the vehicle rather than the calendar (``@`` for a
    low-floor trip) are simply not matched here; a trip carrying only those
    yields a service that runs on no days, and ``Service.service_id`` raises
    rather than emitting an empty calendar entry.
    """
    present = set(codes)
    return Service(
        working_days=WORKING_DAYS in present,
        saturday=SATURDAY in present,
        sunday=SUNDAY_AND_HOLIDAYS in present,
    )


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


def holidays_between(start: dt.date, end: dt.date) -> set[dt.date]:
    """Holidays falling within ``[start, end]``."""
    years = range(start.year, end.year + 1)
    return {d for year in years for d in czech_holidays(year) if start <= d <= end}


def czech_holidays(year: int) -> set[dt.date]:
    """Every public holiday in a given year, fixed and movable.

    Delegated to ``holidays``, which tracks the statute (zákon č. 245/2000 Sb.)
    and computes Easter, rather than carrying a hand-written table that would
    quietly go wrong the year the law or the algorithm did. Checked against the
    previous hand-rolled implementation across 2024-2035: identical.
    """
    return set(holidays.country_holidays("CZ", years=year).keys())
