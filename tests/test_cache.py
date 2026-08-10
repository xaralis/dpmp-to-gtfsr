"""Tests for the development response cache."""

import os
import time

import httpx

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.api.cache import MISS, SETTLED_TTL, VOLATILE_TTL, ResponseCache, ttl_for
from dpmp_gtfs.config import Settings

API = "https://api.mhdonline.cz"


def _settings(tmp_path, **over: object) -> Settings:
    return Settings(
        api_root=API,
        provider="pardubice",
        max_retries=2,
        crawl_rate_limit=1000.0,
        data_dir=tmp_path,
        **over,  # type: ignore[arg-type]
    )


def test_timetable_paths_outlive_live_ones():
    assert ttl_for("stops") == SETTLED_TTL
    assert ttl_for("lines") == SETTLED_TTL
    assert ttl_for("connections/9/115") == SETTLED_TTL
    assert ttl_for("vehicles") == VOLATILE_TTL
    assert ttl_for("events") == VOLATILE_TTL


def test_an_unknown_path_gets_the_cautious_ttl():
    """A future endpoint must be allowed to be five minutes stale, not twelve
    hours: nobody has decided yet how fast it moves."""
    assert ttl_for("departures/16") == VOLATILE_TTL


def test_round_trips_a_payload(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("stops", [{"id": 16}])
    assert cache.get("stops") == [{"id": 16}]


def test_a_cached_404_replays_as_none_not_as_a_miss(tmp_path):
    """The whole point of caching misses is the ~1,600 404s a crawl spends
    finding the end of each line, so ``None`` must be distinguishable from
    "nothing stored"."""
    cache = ResponseCache(tmp_path)
    cache.put("connections/9/9999", None)
    assert cache.get("connections/9/9999") is None
    assert cache.get("connections/9/9998") is MISS


def test_an_entry_past_its_ttl_is_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("vehicles", {"vehicles": []})
    stale = time.time() - VOLATILE_TTL - 1
    entry = cache._entry("vehicles")
    os.utime(entry, (stale, stale))
    assert cache.get("vehicles") is MISS


def test_a_truncated_entry_is_a_miss_rather_than_a_crash(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("stops", [{"id": 16}])
    cache._entry("stops").write_text('{"payload": [', encoding="utf8")
    assert cache.get("stops") is MISS


async def test_the_client_serves_a_second_call_without_touching_the_network(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=[{"id": 16, "name": "Hlavní nádraží"}])

    transport = httpx.MockTransport(handler)
    settings = _settings(tmp_path, http_cache=True)
    for _ in range(2):
        async with (
            httpx.AsyncClient(transport=transport, base_url=API) as raw,
            DpmpApiClient(settings=settings, client=raw) as api,
        ):
            await api._get("stops")

    assert calls == ["/pardubice/stops"]


async def test_the_cache_stays_out_of_the_way_when_disabled(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    settings = _settings(tmp_path)
    for _ in range(2):
        async with (
            httpx.AsyncClient(transport=transport, base_url=API) as raw,
            DpmpApiClient(settings=settings, client=raw) as api,
        ):
            await api._get("stops")

    assert len(calls) == 2
    assert not (tmp_path / "http-cache").exists()
