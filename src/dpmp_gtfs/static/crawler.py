"""Fetches the timetable, guided by the CIS registry.

The API exposes trips one at a time and no longer lists them, so the registry
supplies the list and this module fetches exactly those -- roughly 2,700
requests at 8/s, about six minutes.

The two sources drift: CIS republishes in batches, the API changes when DPMP
changes it. A trip the registry lists and the API answers 404 for is skipped,
because a genuinely cancelled trip looks exactly like that. Too many of them
on one line is a different thing entirely -- it means the wrong version was
selected -- so that fails the build rather than quietly halving a line.
"""

import asyncio
import logging
from typing import Protocol

from dpmp_gtfs.api.models import Connection, Line, Stop
from dpmp_gtfs.cis.index import ServiceIndex
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.types import Timetable

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 30.0
"""Seconds before the first retry, doubling after that. Generous on purpose:
upstream outages last minutes, and a crawl is a nightly job with nobody
waiting on it."""

MISSING_TRIP_LIMIT = 0.05
"""How much of one line's registry may be missing from the API before the
build is treated as wrong rather than merely out of date."""


class SupportsTimetable(Protocol):
    async def stops(self) -> list[Stop]: ...
    async def lines(self) -> list[Line]: ...
    async def connection(self, line: str, number: int) -> Connection | None: ...


async def crawl(
    api: SupportsTimetable,
    index: ServiceIndex,
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
            return await _crawl_once(api, index)
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


async def _crawl_once(api: SupportsTimetable, index: ServiceIndex) -> Timetable:
    stops, lines = await asyncio.gather(api.stops(), api.lines())
    logger.info("crawling %d lines, %d stops", len(lines), len(stops))

    timetable = Timetable(stops=stops, lines=lines)

    for line in lines:
        services = index.lines.get(line.jdf_id)
        if services is None:
            logger.warning(
                "line %s (%s) is not in the CIS registry; it will have no trips",
                line.id,
                line.jdf_id,
            )
            continue

        wanted = sorted(services.trips)
        results = await asyncio.gather(*(api.connection(line.id, n) for n in wanted))

        missing = 0
        for number, connection in zip(wanted, results, strict=True):
            if connection is None:
                missing += 1
                logger.debug("line %s trip %d is in CIS but not in the API", line.id, number)
                continue
            timetable.connections[(line.id, number)] = connection
            timetable.directions[(line.id, number)] = services.trips[number]

        if wanted and missing / len(wanted) > MISSING_TRIP_LIMIT:
            raise DpmpApiError(
                f"line {line.id} ({line.jdf_id}): {missing} of {len(wanted)} registry trips "
                f"are absent from the API -- the CIS version in force is probably not "
                f"{services.valid_from}"
            )
        if missing:
            logger.info("line %s: skipped %d of %d trips", line.id, missing, len(wanted))

    logger.info("crawl complete: %d trips", timetable.trip_count)
    return timetable
