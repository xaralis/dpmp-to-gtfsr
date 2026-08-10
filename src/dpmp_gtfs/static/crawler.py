"""Fetches the whole timetable from the API.

The API exposes trips one at a time and publishes no listing, so for each
line this walks its trip-number space with :func:`discover_trips`, which
returns the connections it found while probing -- no second fetch pass, since
the walk already paid for them -- and derives which way each one runs with
:func:`assign_directions` over exactly those connections. Roughly 2,700 trips
at 8 req/s, about six minutes, plus the discovery walk's own misses, cheap
next to the outage risk of trusting a second source that cannot be kept in
sync (see ``docs/upstream-api.md``).

The days of operation are the exception, and they arrive here already read
from CIS. They are carried through rather than fetched because they are not
the API's to give: its ``fixedCodes`` contradict DPMP's own published
timetable on about a third of trips.
"""

import asyncio
import datetime as dt
import logging
from typing import Protocol

from dpmp_gtfs.api.models import Connection, Line, Stop
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.static.direction import assign_directions
from dpmp_gtfs.static.discovery import discover_trips
from dpmp_gtfs.types import Timetable

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 30.0
"""Seconds before the first retry, doubling after that. Generous on purpose:
upstream outages last minutes, and a crawl is a nightly job with nobody
waiting on it."""


class SupportsTimetable(Protocol):
    async def stops(self) -> list[Stop]: ...
    async def lines(self) -> list[Line]: ...
    async def connection(self, line: str, number: int) -> Connection | None: ...


async def crawl(
    api: SupportsTimetable,
    calendars: dict[tuple[str, int], frozenset[dt.date]],
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
) -> Timetable:
    """Fetch the whole timetable, retrying the crawl as a whole.

    The client already retries individual requests, but that is not enough: a
    crawl is thousands of them spread over minutes, so an outage that outlasts
    one request's retries throws away the entire run.

    Raises :class:`DpmpApiError` once attempts are exhausted rather than
    returning what it managed to collect -- to a consumer, a missing trip is
    indistinguishable from a cancelled one.
    """
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await _crawl_once(api, calendars)
        except DpmpApiError as exc:
            last = exc
            if attempt == attempts:
                break
            delay = backoff * 2 ** (attempt - 1)
            logger.warning(
                "crawl attempt %d/%d failed (%s), retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    raise DpmpApiError(f"crawl failed after {attempts} attempts: {last!r}") from last


async def _crawl_once(
    api: SupportsTimetable, calendars: dict[tuple[str, int], frozenset[dt.date]]
) -> Timetable:
    stops, lines = await asyncio.gather(api.stops(), api.lines())
    logger.info("crawling %d lines, %d stops", len(lines), len(stops))

    timetable = Timetable(stops=stops, lines=lines, calendars=calendars)

    for index, line in enumerate(lines, start=1):
        connections = await discover_trips(api, line.id)

        for number, connection in connections.items():
            timetable.connections[(line.id, number)] = connection
        for number, direction in assign_directions(connections).items():
            timetable.directions[(line.id, number)] = direction

        # The six minutes between the start and completion lines are
        # otherwise silent -- this is what lets an operator tell a crawl in
        # progress from one that has stalled.
        logger.info(
            "line %s: %d trips (%d/%d lines)", line.id, len(connections), index, len(lines)
        )

    logger.info("crawl complete: %d trips", timetable.trip_count)
    return timetable
