"""Tests for the transport layer: request shape, retries, outage behaviour."""

import httpx
import pytest

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.config import Settings
from dpmp_gtfs.exceptions import DpmpApiError

API = "https://api.mhdonline.cz"


def _settings(**over: object) -> Settings:
    return Settings(
        api_root=API,
        provider="pardubice",
        max_retries=2,
        crawl_rate_limit=1000.0,
        **over,  # type: ignore[arg-type]
    )


async def test_sends_the_protocol_header_and_uses_get(vehicles_payload):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=vehicles_payload)

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(transport=transport, base_url=API) as raw,
        DpmpApiClient(settings=_settings(), client=raw) as api,
    ):
        await api.vehicles()

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/pardubice/vehicles"
    assert len(seen[0].headers["X-App-Protocol"]) == 64


async def test_refreshes_the_signature_once_on_401(vehicles_payload):
    codes = [401, 200]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-App-Protocol"])
        code = codes.pop(0)
        return httpx.Response(code, json=vehicles_payload if code == 200 else {})

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(transport=transport, base_url=API) as raw,
        DpmpApiClient(settings=_settings(), client=raw) as api,
    ):
        result = await api.vehicles()

    assert len(seen) == 2
    assert len(result.vehicles) > 0


async def test_connection_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="")

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(transport=transport, base_url=API) as raw,
        DpmpApiClient(settings=_settings(), client=raw) as api,
    ):
        assert await api.connection("1", 999) is None


async def test_one_structurally_invalid_vehicle_does_not_discard_the_snapshot(vehicles_payload):
    """Regression: ``VehiclesResponse.model_validate`` on the whole payload
    rejects every vehicle over one bad record -- the same failure shape the
    ``/stops`` missing-coordinate case had, except here it is a genuine
    production blocker: the realtime feed would freeze on the last good
    snapshot until the upstream happened to fix the one bad record."""
    good_count = len(vehicles_payload["vehicles"])
    broken = {
        **vehicles_payload,
        "vehicles": [
            *vehicles_payload["vehicles"],
            {"vid": "999", "lineId": "9", "connectionId": 1},  # missing gpsLat/gpsLon
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=broken)

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(transport=transport, base_url=API) as raw,
        DpmpApiClient(settings=_settings(), client=raw) as api,
    ):
        result = await api.vehicles()

    assert len(result.vehicles) == good_count
    assert "999" not in {v.vid for v in result.vehicles}


async def test_raises_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(transport=transport, base_url=API) as raw,
        DpmpApiClient(settings=_settings(), client=raw) as api,
    ):
        with pytest.raises(DpmpApiError):
            await api.vehicles()
