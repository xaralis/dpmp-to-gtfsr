"""Finds which trips a line runs, by walking its trip-number space.

The API answers ``connections/{line}/{number}`` for one trip at a time and
publishes no listing. The numbers are sparse -- line 1 runs 206 trips spread
over ids up to 441 -- so the walk cannot stop at the first miss. It stops once
enough consecutive numbers come back empty.

The threshold is measured, not guessed: across the whole network the largest
gap between consecutive trip numbers is 18, so 50 leaves an ample margin while
keeping the tail short.
"""

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_STOP_AFTER = 50
BLOCK = 25
"""How many numbers to probe at once, when the remaining miss allowance is
still that large. Keeps the walk concurrent without ever asking a number past
the stop rule -- the batch shrinks to exactly what is left as the allowance
runs low."""


class SupportsConnection[T](Protocol):
    async def connection(self, line: str, number: int) -> T | None: ...


async def discover_trips[T](
    api: SupportsConnection[T], line_id: str, stop_after: int = DEFAULT_STOP_AFTER
) -> dict[int, T]:
    """Every trip the API answers for, keyed by trip number.

    Returns what the walk already fetched while probing for existence,
    rather than just the numbers -- a second pass to fetch the same
    connections again would double the request count for nothing.
    """
    found: dict[int, T] = {}
    misses = 0
    start = 1

    while misses < stop_after:
        size = min(BLOCK, stop_after - misses)
        block = range(start, start + size)
        results = await asyncio.gather(*(api.connection(line_id, n) for n in block))
        for number, result in zip(block, results, strict=True):
            if result is None:
                misses += 1
            else:
                found[number] = result
                misses = 0
        start += size

    logger.info(
        "line %s: found %d trips up to %d", line_id, len(found), max(found) if found else 0
    )
    return found
