"""Assembles the timetable to build a feed from: CIS journeys at API stops.

``api.mhdonline.cz`` no longer answers for a trip no vehicle is on (403), so the
timetable cannot be walked out of it any more and comes from CIS instead, with
its days of operation. Two things still come from the API, because CIS does not
have them: where the stops are, and which lines DPMP lists -- CIS also carries
902 and 906, which it does not.

CIS names stops, the API numbers them, so the join is by name. The spellings
differ only in punctuation and case (``Slovany,U Přejezdu`` against
``Slovany,u přejezdu``, ``ERA,Zámeček`` against ``ERA Zámeček``), and ``/stops``
also lists aliases.
"""

import asyncio
import logging
from typing import Protocol

from dpmp_gtfs.api.models import LOW_FLOOR, Connection, ConnectionStop, Line, Stop
from dpmp_gtfs.cis import Call, CisTimetable, Journey
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.types import Timetable

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 30.0
"""Seconds before the first retry, doubling after that. Generous on purpose:
upstream outages last minutes, and a build is a nightly job with nobody waiting
on it."""


class SupportsStopsAndLines(Protocol):
    async def stops(self) -> list[Stop]: ...
    async def lines(self) -> list[Line]: ...


async def load_timetable(
    api: SupportsStopsAndLines,
    cis: CisTimetable,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
) -> Timetable:
    """Fetch stops and lines and put the CIS journeys on them.

    The client already retries individual requests, but an outage can outlast
    those. Raises :class:`DpmpApiError` once attempts are exhausted: the
    previous feed is better than none built from half an answer.
    """
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            stops, lines = await asyncio.gather(api.stops(), api.lines())
        except DpmpApiError as exc:
            last = exc
            if attempt == attempts:
                break
            delay = backoff * 2 ** (attempt - 1)
            logger.warning(
                "fetching stops and lines failed, attempt %d/%d (%s), retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
        else:
            return assemble(stops, lines, cis)

    raise DpmpApiError(f"stops and lines failed after {attempts} attempts: {last!r}") from last


def assemble(stops: list[Stop], lines: list[Line], cis: CisTimetable) -> Timetable:
    """Put the journeys of the lines DPMP lists onto the stops it publishes."""
    timetable = Timetable(stops=stops, lines=lines)
    stop_ids = _stop_ids_by_name(stops)
    unknown: set[str] = set()

    line_ids = {line.jdf_id: line.id for line in lines}
    for (jdf_id, number), journey in cis.journeys.items():
        line_id = line_ids.get(jdf_id)
        if line_id is None:
            continue
        key = (line_id, number)
        timetable.connections[key] = _connection(line_id, journey, stop_ids, unknown)
        timetable.directions[key] = journey.direction
        timetable.calendars[(jdf_id, number)] = cis.calendars[(jdf_id, number)]

    for name in sorted(unknown):
        logger.warning("CIS stop %r is not in /stops, the calls at it are dropped", name)
    served = {line_id for line_id, _ in timetable.connections}
    for line in lines:
        if line.id not in served:
            logger.warning("line %s (%s) has no journeys in CIS", line.id, line.jdf_id)

    logger.info("timetable: %d trips on %d lines", timetable.trip_count, len(served))
    return timetable


def _connection(
    line_id: str, journey: Journey, stop_ids: dict[str, int], unknown: set[str]
) -> Connection:
    stops: list[ConnectionStop] = []
    for call in journey.calls:
        stop_id = stop_ids.get(_normalised(call.stop_name))
        if stop_id is None:
            unknown.add(call.stop_name)
            continue
        stops.append(_connection_stop(stop_id, call))
    return Connection(
        lineId=line_id,
        connectionId=journey.number,
        fixedCodes=[LOW_FLOOR] if journey.low_floor else [],
        stops=stops,
    )


def _connection_stop(stop_id: int, call: Call) -> ConnectionStop:
    return ConnectionStop(
        stopId=stop_id,
        departureTime=call.departure,
        arrivalTime=call.arrival,
        on_request=call.on_request,
    )


def _stop_ids_by_name(stops: list[Stop]) -> dict[str, int]:
    """Stop names and aliases, spelled so that the two sources meet.

    Real names go in last, so that a stop's own name is never lost to another
    stop's alias.
    """
    ids: dict[str, int] = {}
    for stop in stops:
        for alias in stop.aliases:
            ids[_normalised(alias)] = stop.id
    for stop in stops:
        ids[_normalised(stop.name)] = stop.id
    return ids


def _normalised(name: str) -> str:
    return "".join(c for c in name if c not in " ,").casefold()
