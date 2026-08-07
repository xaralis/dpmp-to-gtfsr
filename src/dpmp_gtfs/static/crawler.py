"""Fetches the complete timetable from the API.

The upstream exposes trips one at a time, so a full crawl is roughly 2,760
requests (31 lines, ~2,730 trips). That is only run when the timetable
changes, and the client's concurrency gate keeps it gentle -- eight parallel
connections was already enough to make the server time out during exploration.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.api.models import Code, ConnectionDetail, ConnectionSummary, Line, Station

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Timetable:
    """Everything needed to build a static feed."""

    stations: list[Station]
    lines: list[Line]
    codes: list[Code]
    summaries: dict[tuple[int, int], ConnectionSummary] = field(default_factory=dict)
    """Keyed by ``(line_number, connection_number)``."""
    details: dict[tuple[int, int], ConnectionDetail] = field(default_factory=dict)

    @property
    def trip_count(self) -> int:
        return len(self.details)


async def crawl(api: DpmpApiClient) -> Timetable:
    """Fetch the whole timetable.

    Raises if anything fails outright -- a partial timetable must never reach
    the feed, because a missing trip is indistinguishable from a cancelled one
    to a consumer.
    """
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
