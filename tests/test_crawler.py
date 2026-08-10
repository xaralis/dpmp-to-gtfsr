"""Tests for crawling the timetable.

The point of interest is retry behaviour: the client already retries single
requests, but a crawl is ~2,700 of them over several minutes, so an outage
that outlasts one request's retries would otherwise discard the whole run.
The other point of interest is the shape of what comes out: trips discovered
and directions assigned, with nothing left to reconcile against a second
source.
"""

import logging

import pytest

from dpmp_gtfs.api.models import Connection, Line, Stop
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.static.crawler import crawl


class FailingApi:
    """Fails a set number of times before answering, to exercise retry."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.attempts = 0

    async def _maybe_fail(self) -> None:
        if self.attempts <= self.failures:
            raise DpmpApiError("upstream is down")

    async def stops(self) -> list[Stop]:
        self.attempts += 1
        await self._maybe_fail()
        return []

    async def lines(self) -> list[Line]:
        await self._maybe_fail()
        return []

    async def connection(self, line: str, number: int) -> Connection | None:
        raise AssertionError("not reached with no lines")


async def test_a_clean_crawl_makes_one_attempt():
    api = FailingApi()
    timetable = await crawl(api, {}, backoff=0)
    assert api.attempts == 1
    assert timetable.trip_count == 0


async def test_a_transient_outage_is_retried():
    """An outage lasting longer than one request's retries must not cost the
    whole rebuild."""
    api = FailingApi(failures=2)
    await crawl(api, {}, attempts=3, backoff=0)
    assert api.attempts == 3


async def test_a_sustained_outage_raises_rather_than_returning_a_partial_feed():
    """A half-crawled timetable would publish a feed where missing trips look
    cancelled, which is worse than not publishing."""
    api = FailingApi(failures=99)

    with pytest.raises(DpmpApiError, match="failed after 3 attempts"):
        await crawl(api, {}, attempts=3, backoff=0)

    assert api.attempts == 3


async def test_attempts_can_be_disabled():
    api = FailingApi(failures=1)

    with pytest.raises(DpmpApiError):
        await crawl(api, {}, attempts=1, backoff=0)

    assert api.attempts == 1


class FakeApi:
    """A single line with a handful of trips, split into two directions."""

    def __init__(self, present: set[int]):
        self.present = present
        self.asked: list[tuple[str, int]] = []

    async def stops(self) -> list[Stop]:
        return [Stop.model_validate({"id": 1, "name": "A", "gpsLat": 50.0, "gpsLon": 15.0})]

    async def lines(self) -> list[Line]:
        return [Line.model_validate({"id": "1", "jdfId": "655001", "enabled": True})]

    async def connection(self, line: str, number: int) -> Connection | None:
        self.asked.append((line, number))
        if number not in self.present:
            return None
        # Odd trips run one way, even trips the other, so direction
        # assignment has something to do.
        outbound = number % 2 == 1
        stops = [10, 20] if outbound else [20, 10]
        return Connection.model_validate(
            {
                "lineId": line,
                "connectionId": number,
                "fixedCodes": ["X"],
                "stops": [
                    {"stopId": s, "platformId": "1", "departureTime": "04:12:00"} for s in stops
                ],
            }
        )


async def test_crawl_discovers_trips_and_assigns_directions():
    api = FakeApi(present={1, 2, 3, 4})
    table = await crawl(api, {})

    assert table.trip_count == 4
    assert table.stops[0].id == 1
    assert table.lines[0].id == "1"
    assert table.directions[("1", 1)] != table.directions[("1", 2)]
    assert table.directions[("1", 1)] == table.directions[("1", 3)]


async def test_a_line_with_no_trips_yields_none_for_it():
    api = FakeApi(present=set())
    table = await crawl(api, {})

    assert table.trip_count == 0
    assert table.directions == {}


async def test_crawl_logs_progress_per_line(caplog: pytest.LogCaptureFixture) -> None:
    """The start and completion lines already logged bracket several minutes
    of silence on a real crawl; an operator needs something in between to
    tell progress from a stall."""
    api = FakeApi(present={1, 2})

    with caplog.at_level(logging.INFO, logger="dpmp_gtfs.static.crawler"):
        await crawl(api, {})

    assert "line 1: 2 trips (1/1 lines)" in caplog.text
