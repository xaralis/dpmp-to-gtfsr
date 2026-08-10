"""Tests for crawling the timetable.

The point of interest is the join between the two sources: the registry says
which trips exist and which way they run, the API is asked only for those. A
trip the registry lists but the API answers 404 for is skipped, not fatal --
unless too many of one line's trips are missing, in which case that is the
wrong CIS version in force and the build must fail.
"""

import datetime as dt

import pytest

from dpmp_gtfs.cis.index import LineServices, ServiceIndex
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.static.crawler import crawl


class FakeApi:
    """Stands in for the client: answers only for connections in ``present``,
    returning ``None`` (a 404) for everything else."""

    def __init__(self, present: set[tuple[str, int]]):
        self.present = present
        self.asked: list[tuple[str, int]] = []

    async def stops(self):
        from dpmp_gtfs.api.models import Stop

        return [Stop.model_validate({"id": 1, "name": "A", "gpsLat": 50.0, "gpsLon": 15.0})]

    async def lines(self):
        from dpmp_gtfs.api.models import Line

        return [Line.model_validate({"id": "1", "jdfId": "655001", "enabled": True})]

    async def connection(self, line: str, number: int):
        from dpmp_gtfs.api.models import Connection

        self.asked.append((line, number))
        if (line, number) not in self.present:
            return None
        return Connection.model_validate(
            {
                "lineId": line,
                "connectionId": number,
                "fixedCodes": ["X"],
                "stops": [{"stopId": 1, "platformId": "1", "departureTime": "04:12:00"}],
            }
        )


def _index(trips: dict[int, int]) -> ServiceIndex:
    return ServiceIndex(
        lines={"655001": LineServices(jdf_id="655001", valid_from=dt.date(2026, 7, 1), trips=trips)}
    )


async def test_asks_only_for_trips_the_registry_lists():
    api = FakeApi(present={("1", 1), ("1", 3)})
    table = await crawl(api, _index({1: 0, 3: 1}))

    assert sorted(api.asked) == [("1", 1), ("1", 3)]
    assert table.trip_count == 2
    assert table.directions[("1", 3)] == 1


async def test_a_few_missing_trips_are_skipped():
    trips = {n: 0 for n in range(1, 41)}
    api = FakeApi(present={("1", n) for n in range(1, 41)} - {("1", 7)})
    table = await crawl(api, _index(trips))

    assert table.trip_count == 39


async def test_too_many_missing_trips_fails_the_build():
    trips = {n: 0 for n in range(1, 41)}
    api = FakeApi(present={("1", n) for n in range(1, 31)})  # 25% missing
    with pytest.raises(DpmpApiError, match="655001"):
        await crawl(api, _index(trips), attempts=1)


async def test_lines_the_registry_does_not_know_are_skipped():
    api = FakeApi(present=set())
    table = await crawl(api, ServiceIndex(lines={}))
    assert table.trip_count == 0
    assert api.asked == []
