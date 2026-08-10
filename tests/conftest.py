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
