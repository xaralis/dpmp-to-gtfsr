from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


@pytest.fixture
def buses_payload() -> list[dict[str, Any]]:
    return load("buses.json")


@pytest.fixture
def stations_payload() -> list[dict[str, Any]]:
    return load("stations.json")


@pytest.fixture
def detail_payload() -> dict[str, Any]:
    return load("detail-1-1.json")
