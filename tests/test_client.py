"""Tests for the transport layer: request shape, retries, outage behaviour."""

import httpx
import pytest
import respx

from dpmp_gtfs.api import DpmpApiClient, DpmpApiError
from dpmp_gtfs.config import Settings

API = "https://online.dpmp.cz/api"


def make_settings(**overrides: object) -> Settings:
    base = {
        "api_key": "test-key",
        "api_root": API,
        "max_retries": 3,
        "retry_backoff": 0.0,  # keep tests fast
        "crawl_delay": 0.0,
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


@respx.mock
async def test_request_is_posted_as_text_plain_with_the_key() -> None:
    """Regression against the upstream's 500 on application/json.

    The server rejects a correctly-labelled JSON body and accepts the very same
    bytes labelled text/plain.
    """
    route = respx.post(f"{API}/codes").mock(return_value=httpx.Response(200, json=[]))

    async with DpmpApiClient(make_settings()) as api:
        await api.codes()

    request = route.calls.last.request
    assert request.headers["content-type"] == "text/plain;charset=UTF-8"
    assert request.read() == b'{"key": "test-key"}'
    assert request.headers["user-agent"].startswith("dpmp-to-gtfsr/")


@respx.mock
async def test_query_parameters_are_passed_through() -> None:
    route = respx.post(f"{API}/connectionDetail").mock(
        return_value=httpx.Response(200, json={"line_number": 11, "number": 1, "stops": []})
    )

    async with DpmpApiClient(make_settings()) as api:
        await api.connection_detail(line=11, number=1)

    assert dict(route.calls.last.request.url.params) == {"line": "11", "number": "1"}


@respx.mock
async def test_transient_failure_is_retried_then_succeeds() -> None:
    """The upstream intermittently drops connections for minutes at a time."""
    route = respx.post(f"{API}/buses").mock(
        side_effect=[
            httpx.ConnectTimeout("timed out"),
            httpx.Response(500),
            httpx.Response(200, json={"success": True, "data": []}),
        ]
    )

    async with DpmpApiClient(make_settings()) as api:
        assert await api.buses() == []

    assert route.call_count == 3


@respx.mock
async def test_sustained_outage_raises_rather_than_returning_empty() -> None:
    """An outage must be loud.

    Silently returning no vehicles would publish a feed claiming the whole
    fleet had vanished.
    """
    respx.post(f"{API}/buses").mock(side_effect=httpx.ConnectTimeout("down"))

    async with DpmpApiClient(make_settings()) as api:
        with pytest.raises(DpmpApiError, match="failed after 3 attempts"):
            await api.buses()


@respx.mock
async def test_success_false_is_treated_as_an_error() -> None:
    respx.post(f"{API}/buses").mock(
        return_value=httpx.Response(200, json={"success": False, "data": []})
    )

    async with DpmpApiClient(make_settings()) as api:
        with pytest.raises(DpmpApiError, match="success=false"):
            await api.buses()


@respx.mock
async def test_lines_come_back_sorted() -> None:
    respx.post(f"{API}/lines").mock(
        return_value=httpx.Response(
            200, json=[{"number": 11, "stops": []}, {"number": 2, "stops": []}]
        )
    )

    async with DpmpApiClient(make_settings()) as api:
        assert [line.number for line in await api.lines()] == [2, 11]


async def test_missing_key_fails_fast() -> None:
    with pytest.raises(DpmpApiError, match="No API key"):
        DpmpApiClient(make_settings(api_key=""))
