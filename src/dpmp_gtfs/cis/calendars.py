"""Reads the NeTEx archives into "which days does each trip run on".

Deliberately narrow, and narrower than the index this package used to build.
NeTEx also describes trips, stops and directions, but the API answers those
better -- it has platforms, and it knows about trips CIS has not heard of. Days
of operation are the one field where it is the API that is wrong: about a third
of trips carry ``fixedCodes`` that contradict DPMP's own published timetable,
so those come from here instead. Nothing else does.

The days themselves are stored oddly. A ``DayType`` in these files is an empty
element -- no ``DayOfWeek`` at all -- and the calendar lives in the
``ServiceCalendar``, which pairs each ``DayType`` with a ``UicOperatingPeriod``:
a start date plus a string of ``0``/``1``, one character per day from it. That
bitmap is the whole point, because DPMP runs three different weekday patterns
depending on whether schools are in session, and a weekly pattern cannot say
that.

Version selection is the subtle part, and it is decided **per date, not per
line**. A line ships several times over: line 655002 has a year-round version
valid 2026-01-01..2035-12-31 alongside a school-holiday supplement valid
2026-07-01..2026-08-31. Both cover a build date in August, but they do not
compete for the year -- they tile it. Letting the supplement win the whole
window (it has the later ``FromDate``) throws away every day after 31 August
and takes seven routes, lines 2 and 6 among them, dark for ten months of a
feed that claims to be valid for twelve.

So for each date in the window, the version in force *on that date* supplies
that day's bits, and the days are unioned per trip number. Where several
versions still cover the same date, the latest ``FromDate`` wins; a tie goes
to the longer ``ToDate``; a further tie is broken by source file name, purely
so the result does not depend on zip entry order. A trip number the next
version does not carry simply stops running when its own version's turn ends
-- which is exactly what a summer-only trip is.
"""

import datetime as dt
import logging
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring

logger = logging.getLogger(__name__)

NS = {"n": "http://www.netex.org.uk/netex"}
DPMP_OPERATOR = "63217066"

_LINE_NUMBER = re.compile(r":Line:(\d+)")

type Timings = tuple[str, ...]
"""A journey's call times in travel order -- what makes it that journey rather
than another one that happens to carry the same trip number."""


@dataclass(frozen=True, slots=True)
class LineCalendars:
    """One version of one line, and the days each of its trips runs."""

    jdf_id: str
    valid_from: dt.date
    trips: dict[int, frozenset[dt.date]]
    """Trip number -> the days it runs. The trip number is the JDF "spoj",
    which the API reports as ``connectionId`` -- the join between the two
    sources."""

    timings: dict[int, Timings] = field(default_factory=dict)
    """Trip number -> its call times. Only used to decide whether the next
    version still means the same journey by that number."""

    valid_to: dt.date | None = None
    """Upper end of this version's validity window, or ``None`` if the source
    left ``ToDate`` unset (open-ended). Used only to rank versions that share
    a ``FromDate`` -- the longer window wins."""

    source: str = ""
    """Archive file name + zip entry the version was read from. Exists purely
    to make version selection deterministic when two versions still tie on
    ``FromDate`` and ``ToDate``."""


def build_calendars(
    paths: Iterable[Path], on_date: dt.date, horizon: dt.date
) -> dict[tuple[str, int], frozenset[dt.date]]:
    """``(line jdf id, trip number)`` -> the days that trip runs.

    Restricted to ``[on_date, horizon]``; a longer bitmap is clipped.
    """
    versions: dict[str, list[LineCalendars]] = {}
    needle = DPMP_OPERATOR.encode()

    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                blob = archive.read(name)
                # Cheap prefilter: ~36 of 1,043 files are DPMP's, and parsing
                # the rest would cost seconds of XML for nothing.
                if needle not in blob:
                    continue
                parsed = _parse(blob, on_date, horizon, source=f"{path.name}:{name}")
                if parsed is None or not _overlaps(parsed, on_date, horizon):
                    continue
                versions.setdefault(parsed.jdf_id, []).append(parsed)

    calendars: dict[tuple[str, int], frozenset[dt.date]] = {}
    for jdf_id, line in sorted(versions.items()):
        days_by_trip = _days_in_force(line, on_date, horizon)
        _warn_if_coverage_ends_early(jdf_id, days_by_trip, horizon)
        for number, days in days_by_trip.items():
            calendars[(jdf_id, number)] = days

    logger.info(
        "CIS calendars: %d lines, %d trips over %s..%s",
        len(versions),
        len(calendars),
        on_date,
        horizon,
    )
    return calendars


GRACE = dt.timedelta(days=7)
"""How close to the horizon a trip's last day has to be to count as covered.

A trip that only runs on Sundays legitimately stops several days short of an
arbitrary horizon; one that stops months short means CIS has no timetable for
it that far ahead."""


def _warn_if_coverage_ends_early(
    jdf_id: str, days_by_trip: dict[int, frozenset[dt.date]], horizon: dt.date
) -> None:
    """Say so when a line's timetable runs out inside the feed's window.

    It happens whenever the next timetable renumbers its trips: DPMP has filed
    one, but under trip numbers this feed's journeys cannot be matched to, so
    those journeys simply have no days past the changeover. The feed is then
    honestly incomplete rather than wrong, but it is incomplete quietly, and
    an operator watching a line go dark deserves to be told which and when.
    """
    ends = [max(days) for days in days_by_trip.values() if days and max(days) < horizon - GRACE]
    if ends:
        logger.warning(
            "line %s: %d of %d trips have no CIS days after %s -- the next "
            "timetable renumbers them",
            jdf_id,
            len(ends),
            len(days_by_trip),
            max(ends),
        )


def _days_in_force(
    versions: list[LineCalendars], on_date: dt.date, horizon: dt.date
) -> dict[int, frozenset[dt.date]]:
    """Merge one line's versions, letting each own the dates it is in force on.

    Only trip numbers the *current* version has are reported, and a later
    version may extend one only when it still means the same journey by that
    number. Both restrictions are load-bearing: a trip number is not a stable
    identifier across versions. On line 2 the September timetable shares 103
    trip numbers with the August one and gives 45 of them different times, so
    an unconditional union would publish August's departure times running on
    September's days -- a passenger standing at a stop the bus never reaches,
    which is worse than a trip the feed admits it cannot see past.
    """
    winners = [(date, _in_force_on(versions, date)) for date in _days_between(on_date, horizon)]

    owned: dict[int, set[dt.date]] = {}
    for date, index in winners:
        if index is not None:
            owned.setdefault(index, set()).add(date)

    # The version in force at the start of the window is the one whose trip
    # numbering the API is currently serving, so it decides which trips this
    # feed is about at all.
    first = next((index for _, index in winners if index is not None), None)
    if first is None:
        return {}
    current = versions[first]

    # Present even when a trip runs on none of the days it is offered: "CIS
    # knows this trip and says it does not run" is a different answer from
    # "CIS has never heard of it", and only the second sends the builder back
    # to the API's fixed codes.
    trips: dict[int, set[dt.date]] = {number: set() for number in current.trips}
    for index, dates in owned.items():
        version = versions[index]
        for number, days in version.trips.items():
            if number not in trips:
                continue
            if version is not current and not _same_journey(version, current, number):
                continue
            trips[number].update(days & dates)

    return {number: frozenset(days) for number, days in trips.items()}


def _same_journey(later: LineCalendars, current: LineCalendars, number: int) -> bool:
    """Whether two versions still mean the same journey by one trip number.

    Every call time has to match, not just the first: line 9's trip 23 leaves
    at 07:08 either side of the changeover and is a minute apart from the
    eighth stop onwards. A journey with no times at all cannot be shown to be
    the same one, so it is not treated as one -- the whole point of this check
    is that the burden of proof sits on extending a trip, not on stopping it.
    """
    times = later.timings.get(number)
    return bool(times) and times == current.timings.get(number)


def _in_force_on(versions: list[LineCalendars], date: dt.date) -> int | None:
    """Index of the version that governs ``date``, or ``None`` if none does."""
    best: int | None = None
    for index, version in enumerate(versions):
        if not _covers(version, date):
            continue
        if best is None or _sort_key(version) > _sort_key(versions[best]):
            best = index
    return best


def _days_between(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    for offset in range((end - start).days + 1):
        yield start + dt.timedelta(days=offset)


def _covers(line: LineCalendars, date: dt.date) -> bool:
    """Whether ``line``'s validity window includes ``date``."""
    if line.valid_from > date:
        return False
    return line.valid_to is None or line.valid_to >= date


def _overlaps(line: LineCalendars, on_date: dt.date, horizon: dt.date) -> bool:
    """Whether ``line``'s validity window touches the feed's window at all."""
    if line.valid_from > horizon:
        return False
    return line.valid_to is None or line.valid_to >= on_date


def _sort_key(line: LineCalendars) -> tuple[dt.date, dt.date, str]:
    """Rank versions of the same line: latest ``FromDate`` wins; tied, the
    longer ``ToDate`` (open-ended sorts as infinitely long); still tied, the
    source name -- so the winner does not depend on zip iteration order."""
    valid_to = line.valid_to if line.valid_to is not None else dt.date.max
    return (line.valid_from, valid_to, line.source)


def _parse(blob: bytes, on_date: dt.date, horizon: dt.date, source: str) -> LineCalendars | None:
    # defusedxml, not the stdlib parser: this is bulk third-party XML parsed
    # unattended, and entity-expansion attacks are exactly what it is exposed
    # to. Returns an ordinary ElementTree Element, so nothing else changes.
    root = fromstring(blob)

    codes = [e.text for e in root.iterfind(".//n:Operator/n:PublicCode", NS)]
    if DPMP_OPERATOR not in [c for c in codes if c]:
        return None

    line = root.find(".//n:Line", NS)
    if line is None:
        return None
    jdf_id = _line_number(line.get("id") or "")
    valid_from = _date(line.findtext("n:ValidBetween/n:FromDate", namespaces=NS))
    if jdf_id is None or valid_from is None:
        return None

    day_types = _day_type_dates(root, on_date, horizon)

    trips: dict[int, frozenset[dt.date]] = {}
    timings: dict[int, Timings] = {}
    for journey in root.iterfind(".//n:ServiceJourney", NS):
        name = journey.findtext("n:Name", namespaces=NS)
        if name is None or not name.isdigit():
            continue
        days: set[dt.date] = set()
        for ref in journey.iterfind("n:dayTypes/n:DayTypeRef", NS):
            days |= day_types.get(ref.get("ref") or "", frozenset())
        trips[int(name)] = frozenset(days)
        timings[int(name)] = _timings(journey)

    if not trips:
        return None
    return LineCalendars(
        jdf_id=jdf_id,
        valid_from=valid_from,
        trips=trips,
        timings=timings,
        valid_to=_date(line.findtext("n:ValidBetween/n:ToDate", namespaces=NS)),
        source=source,
    )


def _timings(journey: Element) -> Timings:
    """Every time a journey calls at something, in travel order.

    The only handle on whether two versions mean the same journey by a trip
    number. Both times are taken where both exist: the first stop publishes
    only a departure and the last only an arrival, so taking one kind would
    make two journeys that differ at a terminus look identical.
    """
    times: list[str] = []
    for passing in journey.iterfind("n:passingTimes/n:TimetabledPassingTime", NS):
        times.extend(
            text
            for tag in ("ArrivalTime", "DepartureTime")
            if (text := passing.findtext(f"n:{tag}", namespaces=NS))
        )
    return tuple(times)


def _day_type_dates(
    root: Element, on_date: dt.date, horizon: dt.date
) -> dict[str, frozenset[dt.date]]:
    """``DayType`` id -> the days within ``[on_date, horizon]`` it covers."""
    periods = {
        period.get("id") or "": _operating_days(period, on_date, horizon)
        for period in root.iterfind(".//n:UicOperatingPeriod", NS)
    }

    out: dict[str, set[dt.date]] = {}
    for assignment in root.iterfind(".//n:DayTypeAssignment", NS):
        if assignment.findtext("n:isAvailable", namespaces=NS) == "false":
            continue
        day_type = assignment.find("n:DayTypeRef", NS)
        period = assignment.find("n:OperatingPeriodRef", NS)
        if day_type is None or period is None:
            continue
        key = day_type.get("ref") or ""
        out.setdefault(key, set()).update(periods.get(period.get("ref") or "", frozenset()))

    return {key: frozenset(days) for key, days in out.items()}


def _operating_days(period: Element, on_date: dt.date, horizon: dt.date) -> frozenset[dt.date]:
    """Expand a ``UicOperatingPeriod``'s bitmap into the dates it marks."""
    start = _date(period.findtext("n:FromDate", namespaces=NS))
    bits = period.findtext("n:ValidDayBits", namespaces=NS)
    if start is None or not bits:
        return frozenset()

    # The bitmap runs from FromDate to well past the feed's horizon (2030, in
    # the archives seen so far), so it is walked from the first day that can
    # matter rather than from its own start.
    first = max((on_date - start).days, 0)
    last = min((horizon - start).days, len(bits) - 1)
    return frozenset(
        start + dt.timedelta(days=offset)
        for offset in range(first, last + 1)
        if bits[offset] == "1"
    )


def _line_number(ref: str) -> str | None:
    m = _LINE_NUMBER.search(ref)
    return m.group(1) if m else None


def _date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    return dt.datetime.fromisoformat(raw).date()
