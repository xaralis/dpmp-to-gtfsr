"""Reads the NeTEx archives into the whole timetable: journeys and their days.

CIS is what DPMP files its timetables with, and since ``api.mhdonline.cz``
stopped answering for trips nobody is riding (see ``docs/upstream-api.md``) it
is the only place the full timetable can be read at all. Every journey here has
its calls, times, direction and low-floor flag; the only things it lacks are
coordinates and platforms, which is why stops still come from the API.

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
that day's bits. Where several versions cover the same date, the latest
``FromDate`` wins; a tie goes to the shorter ``ToDate``, because the version
that bothers to end is the temporary one laid over the other -- line 21 has both
kinds from 2026-09-01, and the shorter is the one running now. A further tie is
broken by source file name, purely so the result does not depend on zip entry
order.

Trip numbers are not stable across versions: on line 2 the September timetable
shares 103 trip numbers with the August one and gives 45 of them different
times. So a later version only extends a trip when it means the same journey,
call for call. Otherwise its journey is a trip of its own, under the number
suffixed with the date its version starts, and the feed knows the timetable
past a changeover without publishing August's times on September's days.
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

from dpmp_gtfs.types import TripCalendar, TripNumber

logger = logging.getLogger(__name__)

NS = {"n": "http://www.netex.org.uk/netex"}
DPMP_OPERATOR = "63217066"

_LINE_NUMBER = re.compile(r":Line:(\d+)")

OUTBOUND = 0
INBOUND = 1
LOW_FLOOR = "lowFloor"


@dataclass(frozen=True, slots=True)
class Call:
    """One stop of a journey, named as CIS names it."""

    stop_name: str
    arrival: str | None
    """``HH:MM:SS``. The first stop publishes only a departure and the last only
    an arrival."""
    departure: str | None
    on_request: bool = False
    """Zastávka na znamení, on this line in this direction."""


@dataclass(frozen=True, slots=True)
class Journey:
    """One numbered journey of one version of a line."""

    number: int
    calls: tuple[Call, ...]
    direction: int
    low_floor: bool

    def means_the_same_as(self, other: Journey | None) -> bool:
        """Whether two versions still mean one journey by this trip number.

        Every call has to match, not just the first: line 9's trip 23 leaves at
        07:08 either side of the changeover and is a minute apart from the
        eighth stop onwards. A journey with no times at all cannot be shown to
        be the same one, so it is not treated as one -- the burden of proof
        sits on extending a trip, not on stopping it.
        """
        return (
            other is not None
            and any(c.arrival or c.departure for c in self.calls)
            and self.calls == other.calls
        )


@dataclass(slots=True)
class CisTimetable:
    """Everything CIS says about DPMP over one window."""

    calendars: dict[tuple[str, TripNumber], TripCalendar] = field(default_factory=dict)
    """``(line jdf id, trip)`` -> the days it runs."""
    journeys: dict[tuple[str, TripNumber], Journey] = field(default_factory=dict)
    """The same keys -> what it does on those days."""


@dataclass(frozen=True, slots=True)
class LineCalendars:
    """One version of one line, and the days each of its trips runs."""

    jdf_id: str
    valid_from: dt.date
    trips: dict[int, frozenset[dt.date]]
    """Trip number -> the days it runs. The trip number is the JDF "spoj". The
    API reports a ``connectionId`` that usually equals it, but not always, so
    it is not a join -- see :meth:`dpmp_gtfs.realtime.index.StaticIndex.trip_of`."""

    origins: dict[int, str] = field(default_factory=dict)
    """Trip number -> a name for the calendar its days were read from, built
    from the raw bitmaps rather than from the days they expand to. See
    :attr:`dpmp_gtfs.types.TripCalendar.origin`."""

    journeys: dict[int, Journey] = field(default_factory=dict)
    """Trip number -> what it does."""

    valid_to: dt.date | None = None
    """Upper end of this version's validity window, or ``None`` if the source
    left ``ToDate`` unset (open-ended). Ends this version's turn, and ranks
    versions that share a ``FromDate`` -- the shorter window wins."""

    source: str = ""
    """Archive file name + zip entry the version was read from. Exists purely
    to make version selection deterministic when two versions still tie on
    ``FromDate`` and ``ToDate``."""


def read_timetable(paths: Iterable[Path], on_date: dt.date, horizon: dt.date) -> CisTimetable:
    """Every DPMP journey with a day in ``[on_date, horizon]``, and those days.

    A longer bitmap is clipped. Each calendar also carries the name of the
    calendar it was read from, because the days alone move with the window and
    a service needs an identity that does not -- see
    :attr:`dpmp_gtfs.types.TripCalendar.origin`.
    """
    versions: dict[str, list[LineCalendars]] = {}
    needle = DPMP_OPERATOR.encode()

    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                blob = archive.read(name)
                # Cheap prefilter: ~110 of 1,043 files are DPMP's, and parsing
                # the rest would cost seconds of XML for nothing.
                if needle not in blob:
                    continue
                parsed = _parse(blob, on_date, horizon, source=f"{path.name}:{name}")
                if parsed is None or not _overlaps(parsed, on_date, horizon):
                    continue
                versions.setdefault(parsed.jdf_id, []).append(parsed)

    timetable = CisTimetable()
    for jdf_id, line in sorted(versions.items()):
        by_trip = _days_in_force(line, on_date, horizon)
        _warn_if_coverage_ends_early(jdf_id, by_trip, horizon)
        for number, (calendar, journey) in by_trip.items():
            timetable.calendars[(jdf_id, number)] = calendar
            timetable.journeys[(jdf_id, number)] = journey

    logger.info(
        "CIS timetable: %d lines, %d trips over %s..%s",
        len(versions),
        len(timetable.calendars),
        on_date,
        horizon,
    )
    return timetable


GRACE = dt.timedelta(days=14)
"""How close to the horizon a line's last day has to be to count as covered.

A line that only runs in term time, or for an airport's flights, legitimately
stops short of an arbitrary horizon by days; one that stops months short means
CIS has no timetable for it that far ahead."""


def _warn_if_coverage_ends_early(
    jdf_id: str, by_trip: dict[TripNumber, tuple[TripCalendar, Journey]], horizon: dt.date
) -> None:
    """Say so when a line's timetable runs out inside the feed's window.

    The feed is then honestly incomplete rather than wrong, but it is
    incomplete quietly, and an operator watching a line go dark deserves to be
    told which and when.
    """
    last = max((max(c.days) for c, _ in by_trip.values() if c.days), default=None)
    if last is not None and last < horizon - GRACE:
        logger.warning("line %s: CIS has no service after %s", jdf_id, last)


def _days_in_force(
    versions: list[LineCalendars], on_date: dt.date, horizon: dt.date
) -> dict[TripNumber, tuple[TripCalendar, Journey]]:
    """Merge one line's versions, letting each own the dates it is in force on.

    The version in force at the start of the window keeps the plain trip
    numbers, and a later version shares one only by meaning the same journey by
    it. Anything else later gets a number of its own. The plain numbers are
    reported even for a trip that runs on none of the days offered: "CIS knows
    this trip and says it does not run" is an answer too.
    """
    winners = [(date, _in_force_on(versions, date)) for date in _days_between(on_date, horizon)]

    owned: dict[int, set[dt.date]] = {}
    for date, index in winners:
        if index is not None:
            owned.setdefault(index, set()).add(date)

    first = next((index for _, index in winners if index is not None), None)
    if first is None:
        return {}
    current = versions[first]

    days: dict[TripNumber, set[dt.date]] = {number: set() for number in current.trips}
    origins: dict[TripNumber, list[str]] = {number: [] for number in current.trips}
    journeys: dict[TripNumber, Journey] = {
        number: journey for number, journey in current.journeys.items()
    }

    later = sorted((i for i in owned if i != first), key=lambda i: _sort_key(versions[i]))
    for index in [first, *later]:
        version = versions[index]
        for number, trip_days in version.trips.items():
            running = trip_days & owned[index]
            if version is not current and not running:
                continue
            journey = version.journeys[number]
            key = number if version is current else _trip_key(number, journey, version, journeys)
            journeys.setdefault(key, journey)
            days.setdefault(key, set()).update(running)
            origins.setdefault(key, []).append(
                version.origins.get(number, "") + _turn(owned[index], on_date, horizon)
            )

    return {
        key: (TripCalendar(days=frozenset(days[key]), origin="+".join(origins[key])), journeys[key])
        for key in days
    }


def _turn(dates: set[dt.date], on_date: dt.date, horizon: dt.date) -> str:
    """The dates a version is in force on, as far as they name its calendar.

    One bitmap read under two versions' turns is two different sets of days, on
    the same weekly pattern, so an origin has to say whose turn it was. An edge
    that is only the window's own is left out, because it moves every night.
    """
    start, end = min(dates), max(dates)
    return f"@{'' if start == on_date else start}..{'' if end == horizon else end}"


def _trip_key(
    number: int, journey: Journey, version: LineCalendars, known: dict[TripNumber, Journey]
) -> TripNumber:
    """The trip this version's journey belongs to.

    Dated by the version that introduced it, not by anything the window moves:
    a key that changed nightly would rename the trip in the published feed.
    """
    if number not in known or journey.means_the_same_as(known[number]):
        return number
    dated = f"{number}-{version.valid_from:%Y%m%d}"
    # Two versions can start on one date (line 21), and a journey can come back.
    suffix = 0
    key = dated
    while key in known and not journey.means_the_same_as(known[key]):
        suffix += 1
        key = f"{dated}.{suffix}"
    return key


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


def _sort_key(line: LineCalendars) -> tuple[dt.date, int, str]:
    """Rank versions of the same line: latest ``FromDate`` wins; tied, the
    shorter ``ToDate`` (open-ended sorts as infinitely long); still tied, the
    source name -- so the winner does not depend on zip iteration order."""
    valid_to = line.valid_to if line.valid_to is not None else dt.date.max
    return (line.valid_from, -valid_to.toordinal(), line.source)


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
    day_type_bitmaps = _day_type_bitmaps(root)
    points = _journey_pattern_points(root)
    directions = _journey_pattern_directions(root)

    trips: dict[int, frozenset[dt.date]] = {}
    origins: dict[int, str] = {}
    journeys: dict[int, Journey] = {}
    for journey in root.iterfind(".//n:ServiceJourney", NS):
        name = journey.findtext("n:Name", namespaces=NS)
        if name is None or not name.isdigit():
            continue
        days: set[dt.date] = set()
        bitmaps: list[str] = []
        for ref in journey.iterfind("n:dayTypes/n:DayTypeRef", NS):
            key = ref.get("ref") or ""
            days |= day_types.get(key, frozenset())
            bitmaps.extend(day_type_bitmaps.get(key, ()))
        trips[int(name)] = frozenset(days)
        origins[int(name)] = ",".join(sorted(bitmaps))
        journeys[int(name)] = _journey(int(name), journey, points, directions)

    if not trips:
        return None
    return LineCalendars(
        jdf_id=jdf_id,
        valid_from=valid_from,
        trips=trips,
        origins=origins,
        journeys=journeys,
        valid_to=_date(line.findtext("n:ValidBetween/n:ToDate", namespaces=NS)),
        source=source,
    )


def _day_type_bitmaps(root: Element) -> dict[str, tuple[str, ...]]:
    """``DayType`` id -> the raw operating periods behind it.

    The same pairing :func:`_day_type_dates` walks, but kept as the archive
    spells it -- ``FromDate`` and the bitmap, neither of which moves when the
    feed's window does. Expanding the bitmap answers *which days*; this answers
    *which calendar*, and only the second one can name a service.
    """
    periods = {
        period.get("id") or "": (
            f"{period.findtext('n:FromDate', namespaces=NS)}:"
            f"{period.findtext('n:ValidDayBits', namespaces=NS)}"
        )
        for period in root.iterfind(".//n:UicOperatingPeriod", NS)
    }

    out: dict[str, list[str]] = {}
    for assignment in root.iterfind(".//n:DayTypeAssignment", NS):
        if assignment.findtext("n:isAvailable", namespaces=NS) == "false":
            continue
        day_type = assignment.find("n:DayTypeRef", NS)
        period = assignment.find("n:OperatingPeriodRef", NS)
        if day_type is None or period is None:
            continue
        if raw := periods.get(period.get("ref") or ""):
            out.setdefault(day_type.get("ref") or "", []).append(raw)

    return {key: tuple(sorted(raw)) for key, raw in out.items()}


def _journey_pattern_points(root: Element) -> dict[str, tuple[str, bool]]:
    """``StopPointInJourneyPattern`` id -> the stop's name, and if on request."""
    names = {
        point.get("id") or "": point.findtext("n:Name", default="", namespaces=NS)
        for point in root.iterfind(".//n:ScheduledStopPoint", NS)
    }
    out: dict[str, tuple[str, bool]] = {}
    for point in root.iterfind(".//n:StopPointInJourneyPattern", NS):
        scheduled = point.find("n:ScheduledStopPointRef", NS)
        name = names.get(scheduled.get("ref") or "", "") if scheduled is not None else ""
        on_request = point.findtext("n:RequestStop", namespaces=NS) == "true"
        out[point.get("id") or ""] = (name, on_request)
    return out


def _journey_pattern_directions(root: Element) -> dict[str, int]:
    """``ServiceJourneyPattern`` id -> ``direction_id``."""
    out: dict[str, int] = {}
    for pattern in root.iterfind(".//n:ServiceJourneyPattern", NS):
        direction = pattern.find("n:DirectionRef", NS)
        inbound = direction is not None and (direction.get("ref") or "").endswith(":in")
        out[pattern.get("id") or ""] = INBOUND if inbound else OUTBOUND
    return out


def _journey(
    number: int,
    journey: Element,
    points: dict[str, tuple[str, bool]],
    directions: dict[str, int],
) -> Journey:
    calls: list[Call] = []
    for passing in journey.iterfind("n:passingTimes/n:TimetabledPassingTime", NS):
        point = passing.find("n:StopPointInJourneyPatternRef", NS)
        ref = (point.get("ref") or "") if point is not None else ""
        name, on_request = points.get(ref, ("", False))
        calls.append(
            Call(
                stop_name=name,
                arrival=passing.findtext("n:ArrivalTime", namespaces=NS),
                departure=passing.findtext("n:DepartureTime", namespaces=NS),
                on_request=on_request,
            )
        )
    pattern = journey.find("n:ServiceJourneyPatternRef", NS)
    facilities = journey.findtext(".//n:MobilityFacilityList", default="", namespaces=NS)
    return Journey(
        number=number,
        calls=tuple(calls),
        direction=directions.get(pattern.get("ref") or "" if pattern is not None else "", OUTBOUND),
        low_floor=LOW_FLOOR in facilities,
    )


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
