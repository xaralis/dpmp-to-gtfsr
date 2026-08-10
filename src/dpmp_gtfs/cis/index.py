"""Reads the NeTEx archives into "which trips exist, and which way they run".

Deliberately narrow. NeTEx can also describe stop times and calendars, but the
API answers those better -- it has platforms, which CIS does not -- and two
competing descriptions of the same trip would mean deciding which one wins on
every field. So nothing but trip numbers and directions crosses this boundary.

Version selection is the subtle part. A line is usually present several times
over, and more than one version is typically valid *today*: line 655001 ships
as a 283-trip version valid from 2026-01-01 and a 206-trip version valid from
2026-07-01, and the API agrees with the latter exactly. Unioning them would
invent 141 trips that do not run, so the rule is: among versions whose window
actually covers the build date (``FromDate <= on_date`` and ``ToDate`` absent
or ``>= on_date``), the latest ``FromDate`` wins; a tie goes to the longer
``ToDate``; a further tie is broken by source file name, purely so the result
is deterministic rather than an accident of zip entry order.
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
_PATTERN_KEY = re.compile(r":ServiceJourneyPattern:(.+)$")

OUTBOUND = 0
INBOUND = 1


@dataclass(frozen=True, slots=True)
class LineServices:
    """One line's trips, as of a date."""

    jdf_id: str
    valid_from: dt.date
    trips: dict[int, int]
    """Trip number -> direction_id. The trip number is the JDF "spoj", which
    the API reports as ``connectionId`` -- the join between the two sources."""

    valid_to: dt.date | None = None
    """Upper end of this version's validity window, or ``None`` if the source
    left ``ToDate`` unset (open-ended). Used only to rank versions that share
    a ``FromDate`` -- the longer window wins."""

    source: str = ""
    """Archive file name + zip entry the version was read from, e.g.
    ``"linky.zip:655001_2026.xml"``. Exists purely to make version selection
    deterministic when two versions still tie on ``FromDate`` and ``ToDate``
    -- it is a tiebreaker, not something meant for display."""


@dataclass(frozen=True, slots=True)
class ServiceIndex:
    lines: dict[str, LineServices]
    """Keyed by JDF line number, e.g. ``"655001"``."""

    @property
    def trip_count(self) -> int:
        return sum(len(line.trips) for line in self.lines.values())


def build_index(
    paths: Iterable[Path],
    on_date: dt.date,
    operator: str = DPMP_OPERATOR,
) -> ServiceIndex:
    """Read every archive and keep, per line, the version in force on ``on_date``."""
    best: dict[str, LineServices] = {}
    needle = operator.encode()

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
                parsed = _parse(blob, operator, source=f"{path.name}:{name}")
                if parsed is None:
                    continue
                if not _covers(parsed, on_date):
                    continue
                current = best.get(parsed.jdf_id)
                if current is None or _sort_key(parsed) > _sort_key(current):
                    best[parsed.jdf_id] = parsed

    logger.info(
        "CIS index: %d lines, %d trips as of %s",
        len(best),
        sum(len(line.trips) for line in best.values()),
        on_date,
    )
    return ServiceIndex(lines=best)


def _covers(line: LineServices, on_date: dt.date) -> bool:
    """Whether ``line``'s validity window has started and not yet ended."""
    if line.valid_from > on_date:
        return False
    return line.valid_to is None or line.valid_to >= on_date


def _sort_key(line: LineServices) -> tuple[dt.date, dt.date, str]:
    """Rank versions of the same line: latest ``FromDate`` wins; tied, the
    longer ``ToDate`` (open-ended sorts as infinitely long); still tied, the
    source name -- so the winner does not depend on zip iteration order."""
    valid_to = line.valid_to if line.valid_to is not None else dt.date.max
    return (line.valid_from, valid_to, line.source)


def _parse(blob: bytes, operator: str, source: str) -> LineServices | None:
    # defusedxml, not the stdlib parser: this is bulk third-party XML parsed
    # unattended, and entity-expansion attacks are exactly what it is exposed
    # to. Returns an ordinary ElementTree Element, so nothing else changes.
    root = fromstring(blob)

    codes = [e.text for e in root.iterfind(".//n:Operator/n:PublicCode", NS)]
    if operator not in [c for c in codes if c]:
        return None

    line = root.find(".//n:Line", NS)
    if line is None:
        return None
    jdf_id = _line_number(line.get("id") or "")
    valid_from = _valid_from(line)
    if jdf_id is None or valid_from is None:
        return None
    valid_to = _valid_to(line)

    directions = _pattern_directions(root)

    trips: dict[int, int] = {}
    for journey in root.iterfind(".//n:ServiceJourney", NS):
        name = journey.findtext("n:Name", namespaces=NS)
        ref = journey.find("n:ServiceJourneyPatternRef", NS)
        if name is None or not name.isdigit() or ref is None:
            continue
        key = _pattern_key(ref.get("ref") or "") or ""
        if key not in directions:
            logger.warning(
                "line %s trip %s: unresolved ServiceJourneyPatternRef %r, "
                "defaulting to OUTBOUND",
                jdf_id,
                name,
                ref.get("ref"),
            )
        trips[int(name)] = directions.get(key, OUTBOUND)

    if not trips:
        return None
    return LineServices(
        jdf_id=jdf_id,
        valid_from=valid_from,
        trips=trips,
        valid_to=valid_to,
        source=source,
    )


def _pattern_directions(root: Element) -> dict[str, int]:
    """``{"1_out": 0, "2_in": 1}`` -- the direction each journey pattern runs.

    The old API gave each stop an ``index`` into the line's canonical ordering
    and direction was read off whether a trip walked it up or down. That field
    is gone; this is its replacement, and it is a stated direction rather than
    an inferred one.
    """
    out: dict[str, int] = {}
    for pattern in root.iterfind(".//n:ServiceJourneyPattern", NS):
        key = _pattern_key(pattern.get("id") or "")
        if key is None:
            continue
        ref = pattern.find("n:DirectionRef", NS)
        target = (ref.get("ref") or "") if ref is not None else ""
        out[key] = INBOUND if target.endswith(":in") else OUTBOUND
    return out


def _pattern_key(ref: str) -> str | None:
    m = _PATTERN_KEY.search(ref)
    return m.group(1) if m else None


def _line_number(ref: str) -> str | None:
    m = _LINE_NUMBER.search(ref)
    return m.group(1) if m else None


def _valid_from(line: Element) -> dt.date | None:
    raw = line.findtext("n:ValidBetween/n:FromDate", namespaces=NS)
    if not raw:
        return None
    return dt.datetime.fromisoformat(raw).date()


def _valid_to(line: Element) -> dt.date | None:
    raw = line.findtext("n:ValidBetween/n:ToDate", namespaces=NS)
    if not raw:
        return None
    return dt.datetime.fromisoformat(raw).date()
