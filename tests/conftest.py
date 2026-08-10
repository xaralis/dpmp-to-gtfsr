import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


@pytest.fixture
def stub_cis(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, int], frozenset[dt.date]]:
    """Answer the scheduler's CIS step from memory.

    Every build now starts by downloading ~300 MB of NeTEx, which no test of
    the scheduler is about. The returned dict is what the stub hands back, so
    a test that does care can put trips in it. Deliberately not empty: the
    scheduler refuses to build a feed on no calendars at all.
    """
    from dpmp_gtfs.web import scheduler as scheduler_module

    calendars = {("655001", 1): frozenset({dt.date(2026, 8, 10)})}

    async def fetch_archives(urls: Any, dest: Any, client: Any = None) -> list[Path]:
        return []

    def build_calendars(paths: Any, on_date: dt.date, horizon: dt.date) -> Any:
        return calendars

    monkeypatch.setattr(scheduler_module, "fetch_archives", fetch_archives)
    monkeypatch.setattr(scheduler_module, "build_calendars", build_calendars)
    return calendars


@pytest.fixture
def vehicles_payload() -> dict[str, Any]:
    return load("vehicles.json")


@pytest.fixture
def stops_payload() -> list[dict[str, Any]]:
    return load("stops.json")


@pytest.fixture
def lines_payload() -> list[dict[str, Any]]:
    return load("lines.json")


@pytest.fixture
def connection_payload() -> dict[str, Any]:
    return load("connection-1-1.json")


@pytest.fixture
def simple_timetable():
    from dpmp_gtfs.api.models import Connection, Line, Stop
    from dpmp_gtfs.types import Timetable

    stops = [
        Stop.model_validate(
            {"id": 1, "name": "První", "gpsLat": 50.01, "gpsLon": 15.77, "fixedCodes": ["@"]}
        ),
        Stop.model_validate({"id": 2, "name": "Druhá", "gpsLat": 50.02, "gpsLon": 15.78}),
    ]
    lines = [Line.model_validate({"id": "1", "jdfId": "655001", "enabled": True})]

    def connection(number: int, first: int, second: int) -> Connection:
        return Connection.model_validate(
            {
                "lineId": "1",
                "connectionId": number,
                "fixedCodes": ["X", "@"],
                "stops": [
                    {"stopId": first, "platformId": "1", "departureTime": "04:12:00"},
                    {"stopId": second, "platformId": "2", "departureTime": "04:20:00"},
                ],
            }
        )

    return Timetable(
        stops=stops,
        lines=lines,
        directions={("1", 1): 0, ("1", 2): 1},
        connections={("1", 1): connection(1, 1, 2), ("1", 2): connection(2, 2, 1)},
    )


@pytest.fixture
def static_index(simple_timetable, tmp_path):
    """A real ``StaticIndex``, built and read back the same way the scheduler
    does: :func:`build_feed` then :func:`write_feed` then
    ``StaticIndex.from_zip`` -- not a hand-built dict of dataclasses. Line
    ``"1"`` connection ``1`` resolves to trip ``L1C1``."""
    from dpmp_gtfs.realtime.index import StaticIndex
    from dpmp_gtfs.static.builder import build_feed
    from dpmp_gtfs.static.writer import write_feed

    feed = build_feed(simple_timetable)
    destination = tmp_path / "gtfs.zip"
    write_feed(feed, destination)
    return StaticIndex.from_zip(destination)
