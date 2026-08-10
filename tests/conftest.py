import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


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
