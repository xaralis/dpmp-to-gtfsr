"""Service calendars: the days each trip runs, and the GTFS rows that say so.

Two ways in. :func:`service_from_dates` takes the day-by-day list CIS
publishes and squeezes it into a weekly pattern plus exceptions; that is what
the build normally uses. :func:`service_from_codes` reads a trip's JDF fixed
codes instead, and stands in for the handful of trips CIS has never heard of.
The codes are the weaker source -- about a third of them contradict DPMP's own
published timetable, which is why CIS came back -- but for a trip with no
calendar at all they beat nothing.

Case is significant in the codes. Upper-case ``X`` on a trip means "runs on
weekdays"; lower-case ``x`` is a *stop*-level marker meaning "request stop" and
must never be read as a calendar code.
"""

import datetime as dt
from collections.abc import Iterable, Iterator
from dataclasses import replace

import holidays

from dpmp_gtfs.api.models import PER_WEEKDAY, SUNDAY_AND_HOLIDAYS, WORKING_DAYS
from dpmp_gtfs.types import WORKING_WEEK, CalendarException, Service


def service_from_codes(codes: Iterable[str]) -> Service:
    """Read a trip's fixed codes as the days it runs.

    Three kinds of code contribute: ``X`` for the whole working week, ``+`` for
    Sundays and state holidays, and the single-weekday codes ``1``..``7``. The
    last kind is rare -- only the airport shuttle uses it -- but ignoring it
    left those trips running on no days at all.

    Codes describing the vehicle rather than the calendar (``@`` for a
    low-floor trip) are simply not matched here; a trip carrying only those
    yields a service that runs on no days, and ``Service.service_id`` raises
    rather than emitting an empty calendar entry.
    """
    present = set(codes)
    days: set[int] = set()
    if WORKING_DAYS in present:
        days |= WORKING_WEEK
    if SUNDAY_AND_HOLIDAYS in present:
        days.add(6)
    days |= {PER_WEEKDAY[code] for code in present & PER_WEEKDAY.keys()}

    return Service(days=frozenset(days), holidays=SUNDAY_AND_HOLIDAYS in present)


def service_from_dates(dates: frozenset[dt.date], start: dt.date, end: dt.date) -> Service:
    """The smallest weekly pattern plus exceptions that describes ``dates`` exactly.

    A weekday joins the pattern when the service runs on most of that weekday's
    ordinary occurrences in ``[start, end]``; state holidays are counted
    separately, since they follow :attr:`Service.holidays` rather than the day
    they land on. Whatever the pattern then gets wrong is spelled out
    day by day, so the description is exact however irregular the source is.
    """
    holiday_dates = observed_holidays(start, end)
    ordinary = [d for d in days_between(start, end) if d not in holiday_dates]

    weekdays: set[int] = set()
    for weekday in range(7):
        occurrences = [d for d in ordinary if d.weekday() == weekday]
        if occurrences and 2 * sum(d in dates for d in occurrences) > len(occurrences):
            weekdays.add(weekday)

    pattern = Service(
        days=frozenset(weekdays),
        holidays=2 * sum(d in dates for d in holiday_dates) > len(holiday_dates),
    )

    added: set[dt.date] = set()
    removed: set[dt.date] = set()
    for date in days_between(start, end):
        if pattern.runs_on(date, holiday=date in holiday_dates) == (date in dates):
            continue
        (added if date in dates else removed).add(date)

    return replace(pattern, added=frozenset(added), removed=frozenset(removed))


def numbered_services(services: Iterable[Service]) -> dict[Service, Service]:
    """Each service, mapped to the same service with a distinct ``service_id``.

    Several services usually share a weekly pattern -- term time and the school
    holidays are both ``wd``, and differ only in which days they take off -- so
    something has to tell them apart. That something is a number rather than a
    digest of their exception days, because the days themselves are not stable:
    the feed's window slides forward every night, and a hash over dates inside
    it would rename a service every time one of its days fell off the back. A
    number derived from the *order* of the variants survives that, so a feed
    diff shows the days that changed rather than every trip on the network.

    The variant with no exceptions at all, if there is one, keeps the bare name
    -- it is the ordinary case and deserves the ordinary id.
    """
    groups: dict[str, list[Service]] = {}
    for service in services:
        groups.setdefault(service.base_id, []).append(service)

    numbered: dict[Service, Service] = {}
    for group in groups.values():
        variants = sorted(
            (s for s in group if s.added or s.removed),
            key=lambda s: (sorted(s.added), sorted(s.removed)),
        )
        numbered.update({s: s for s in group if not (s.added or s.removed)})
        numbered.update(
            {s: replace(s, variant=number) for number, s in enumerate(variants, start=1)}
        )
    return numbered


def calendar_exceptions(
    services: Iterable[Service], start: dt.date, end: dt.date
) -> Iterator[CalendarException]:
    """Emit the ``calendar_dates.txt`` rows that reconcile ``calendar.txt``
    with what each service actually does.

    ``calendar.txt`` can only say "these weekdays", so every date on which a
    service departs from its own weekly pattern needs a row here. Two things
    cause that: a state holiday, when the network runs its Sunday timetable
    whatever weekday it is, and a service's own :attr:`Service.added` /
    :attr:`Service.removed` days.
    """
    services = list(services)
    holiday_dates = observed_holidays(start, end)

    for date in days_between(start, end):
        holiday = date in holiday_dates
        for service in services:
            runs = service.runs_on(date, holiday=holiday)
            if runs != (date.weekday() in service.days):
                yield CalendarException(service.service_id, date, added=runs)


def observed_holidays(start: dt.date, end: dt.date) -> frozenset[dt.date]:
    """Holidays in ``[start, end]`` that change what the network does.

    A holiday falling on a Sunday is left out: the network is already running
    its Sunday timetable that day, so treating it as a holiday would only
    generate exceptions that cancel each other out.
    """
    return frozenset(d for d in holidays_between(start, end) if d.weekday() != 6)


def days_between(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    """Every date in ``[start, end]``, both ends included."""
    for offset in range((end - start).days + 1):
        yield start + dt.timedelta(days=offset)


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
