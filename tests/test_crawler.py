"""Tests for crawling the timetable.

The point of interest is retry behaviour: the client already retries single
requests, but a crawl is ~2,760 of them over several minutes, so an outage
that outlasts one request's retries would otherwise discard the whole run.
"""

from typing import Any

import pytest

from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.static.crawler import crawl


class FakeApi:
    """Stands in for the client, failing a set number of times first."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.attempts = 0

    async def _maybe_fail(self) -> None:
        if self.attempts <= self.failures:
            raise DpmpApiError("upstream is down")

    async def stations(self) -> list[Any]:
        self.attempts += 1
        await self._maybe_fail()
        return []

    async def lines(self) -> list[Any]:
        await self._maybe_fail()
        return []

    async def codes(self) -> list[Any]:
        await self._maybe_fail()
        return []

    async def connections(self, line: int) -> list[Any]:
        return []

    async def connection_detail(self, line: int, number: int) -> Any:
        raise AssertionError("not reached with no lines")


async def test_a_clean_crawl_makes_one_attempt() -> None:
    api = FakeApi()
    timetable = await crawl(api, backoff=0)  # type: ignore[arg-type]
    assert api.attempts == 1
    assert timetable.trip_count == 0


async def test_a_transient_outage_is_retried() -> None:
    """An outage lasting longer than one request's retries must not cost the
    whole rebuild."""
    api = FakeApi(failures=2)
    await crawl(api, attempts=3, backoff=0)  # type: ignore[arg-type]
    assert api.attempts == 3


async def test_a_sustained_outage_raises_rather_than_returning_a_partial_feed() -> None:
    """A half-crawled timetable would publish a feed where missing trips look
    cancelled, which is worse than not publishing."""
    api = FakeApi(failures=99)

    with pytest.raises(DpmpApiError, match="failed after 3 attempts"):
        await crawl(api, attempts=3, backoff=0)  # type: ignore[arg-type]

    assert api.attempts == 3


async def test_attempts_can_be_disabled() -> None:
    api = FakeApi(failures=1)

    with pytest.raises(DpmpApiError):
        await crawl(api, attempts=1, backoff=0)  # type: ignore[arg-type]

    assert api.attempts == 1
