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

Version selection is the subtle part. A line is usually present several times
over, and more than one version is typically valid *today*: line 655001 ships
as a 283-trip version valid from 2026-01-01 and a 206-trip version valid from
2026-07-01, and the API agrees with the latter exactly. So the rule is: among
versions whose window actually covers the build date (``FromDate <= on_date``
and ``ToDate`` absent or ``>= on_date``), the latest ``FromDate`` wins; a tie
goes to the longer ``ToDate``; a further tie is broken by source file name,
purely so the result is deterministic rather than an accident of zip entry
order.
"""

import datetime as dt
import logging
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring

logger = logging.getLogger(__name__)

NS = {"n": "http://www.netex.org.uk/netex"}
DPMP_OPERATOR = "63217066"

_LINE_NUMBER = re.compile(r":Line:(\d+)")


@dataclass(frozen=True, slots=True)
class LineCalendars:
    """One version of one line, and the days each of its trips runs."""

    jdf_id: str
    valid_from: dt.date
    trips: dict[int, frozenset[dt.date]]
    """Trip number -> the days it runs. The trip number is the JDF "spoj",
    which the API reports as ``connectionId`` -- the join between the two
    sources."""

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
    best: dict[str, LineCalendars] = {}
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
                if parsed is None or not _covers(parsed, on_date):
                    continue
                current = best.get(parsed.jdf_id)
                if current is None or _sort_key(parsed) > _sort_key(current):
                    best[parsed.jdf_id] = parsed

    calendars = {
        (line.jdf_id, number): days for line in best.values() for number, days in line.trips.items()
    }
    logger.info(
        "CIS calendars: %d lines, %d trips over %s..%s",
        len(best),
        len(calendars),
        on_date,
        horizon,
    )
    return calendars


def _covers(line: LineCalendars, on_date: dt.date) -> bool:
    """Whether ``line``'s validity window has started and not yet ended."""
    if line.valid_from > on_date:
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
    for journey in root.iterfind(".//n:ServiceJourney", NS):
        name = journey.findtext("n:Name", namespaces=NS)
        if name is None or not name.isdigit():
            continue
        days: set[dt.date] = set()
        for ref in journey.iterfind("n:dayTypes/n:DayTypeRef", NS):
            days |= day_types.get(ref.get("ref") or "", frozenset())
        trips[int(name)] = frozenset(days)

    if not trips:
        return None
    return LineCalendars(
        jdf_id=jdf_id,
        valid_from=valid_from,
        trips=trips,
        valid_to=_date(line.findtext("n:ValidBetween/n:ToDate", namespaces=NS)),
        source=source,
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
