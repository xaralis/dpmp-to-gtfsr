"""Fetches the complete timetable from the API.

The upstream exposes trips one at a time, so a full crawl is roughly 2,760
requests (31 lines, ~2,730 trips). That is only run when the timetable
changes, and the client's concurrency gate keeps it gentle -- eight parallel
connections was already enough to make the server time out during exploration.
"""

import asyncio
import logging

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.types import Timetable

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 30.0
"""Seconds before the first retry, doubling after that. Generous on purpose:
the upstream's outages last minutes, and a crawl is a nightly job with nobody
waiting on it."""


async def crawl(
    api: DpmpApiClient,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
) -> Timetable:
    """Fetch the whole timetable, retrying the crawl as a whole.

    The client already retries individual requests, but that is not enough
    here: a crawl is ~2,760 of them spread over minutes, so an outage that
    outlasts one request's retries throws away the entire run. Retrying at
    this level costs a few repeated requests and saves the rebuild.

    Raises :class:`DpmpApiError` once attempts are exhausted rather than
    returning what it managed to collect -- to a consumer, a missing trip is
    indistinguishable from a cancelled one.
    """
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await _crawl_once(api)
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


async def _crawl_once(api: DpmpApiClient) -> Timetable:
    stations, lines, codes = await asyncio.gather(api.stations(), api.lines(), api.codes())
    logger.info("crawling %d lines, %d stations", len(lines), len(stations))

    timetable = Timetable(stations=stations, lines=lines, codes=codes)
    summaries = await asyncio.gather(*(api.connections(line.number) for line in lines))

    for line, conns in zip(lines, summaries, strict=True):
        for conn in conns:
            timetable.summaries[(line.number, conn.number)] = conn

    logger.info("found %d trips, fetching stop times", len(timetable.summaries))

    keys = list(timetable.summaries)
    details = await asyncio.gather(*(api.connection_detail(line, num) for line, num in keys))
    timetable.details = dict(zip(keys, details, strict=True))

    logger.info("crawl complete: %d trips with stop times", timetable.trip_count)
    return timetable
