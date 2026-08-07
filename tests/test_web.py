"""Tests for the HTTP layer.

These drive the ASGI app directly rather than through TestClient, so the
lifespan never runs and no background loop reaches the live API.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from google.transit import gtfs_realtime_pb2 as rt

from dpmp_gtfs.config import Settings
from dpmp_gtfs.web.app import create_app
from dpmp_gtfs.web.scheduler import read_feed_version


def _settings(tmp_path: Path) -> Settings:
    return Settings(api_key="test-key", data_dir=tmp_path)  # type: ignore[call-arg]


def _write_minimal_zip(path: Path, version: str = "20260807-abc") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("trips.txt", "route_id,service_id,trip_id\nL9,wd,L9C115\n")
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "L9C115,19:00:00,19:00:00,S1P1,0\n",
        )
        zf.writestr(
            "feed_info.txt",
            f"feed_publisher_name,feed_lang,feed_version\nx,cs,{version}\n",
        )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


# --- feeds ------------------------------------------------------------------


async def test_static_feed_is_503_before_it_is_built(client: httpx.AsyncClient) -> None:
    """Better an explicit 'not ready' than an empty archive that looks valid."""
    response = await client.get("/gtfs.zip")
    assert response.status_code == 503


async def test_static_feed_is_served_with_an_etag(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _write_minimal_zip(tmp_path / "gtfs.zip")

    response = await client.get("/gtfs.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["etag"]
    assert "max-age" in response.headers["cache-control"]


async def test_unchanged_static_feed_answers_304(client: httpx.AsyncClient, tmp_path: Path) -> None:
    _write_minimal_zip(tmp_path / "gtfs.zip")
    etag = (await client.get("/gtfs.zip")).headers["etag"]

    again = await client.get("/gtfs.zip", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


async def test_realtime_is_503_before_the_first_refresh(client: httpx.AsyncClient) -> None:
    """An empty protobuf would claim no vehicles are running."""
    assert (await client.get("/gtfs-rt.pb")).status_code == 503
    assert (await client.get("/gtfs-rt.json")).status_code == 503


async def test_realtime_is_served_as_protobuf(client: httpx.AsyncClient) -> None:
    message = rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = 1786131319
    state = client.app.state.scheduler.state  # type: ignore[attr-defined]
    state.realtime_message = message
    state.realtime = message.SerializeToString()
    state.realtime_built_at = dt.datetime.now(dt.UTC)

    response = await client.get("/gtfs-rt.pb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"

    parsed = rt.FeedMessage()
    parsed.ParseFromString(response.content)
    assert parsed.header.gtfs_realtime_version == "2.0"


# --- status -----------------------------------------------------------------


async def test_healthz_reports_503_when_nothing_is_loaded(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["healthy"] is False


async def test_healthz_is_healthy_with_a_feed_loaded_from_disk(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Regression: health once required having built the static feed in this
    process, so a service running correctly off a feed loaded at startup
    reported 503 until its first nightly rebuild."""
    from dpmp_gtfs.realtime.index import StaticIndex

    _write_minimal_zip(tmp_path / "gtfs.zip")
    state = client.app.state.scheduler.state  # type: ignore[attr-defined]
    state.index = StaticIndex.from_zip(tmp_path / "gtfs.zip")
    state.realtime_built_at = dt.datetime.now(dt.UTC)

    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["healthy"] is True


async def test_a_stale_realtime_feed_is_unhealthy(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    from dpmp_gtfs.realtime.index import StaticIndex

    _write_minimal_zip(tmp_path / "gtfs.zip")
    state = client.app.state.scheduler.state  # type: ignore[attr-defined]
    state.index = StaticIndex.from_zip(tmp_path / "gtfs.zip")
    state.realtime_built_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)

    assert (await client.get("/healthz")).status_code == 503


# --- pages ------------------------------------------------------------------


async def test_homepage_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "GTFS" in response.text
    # The disclaimer must survive template edits.
    assert "Neoficiální" in response.text


async def test_documentation_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/docs")
    assert response.status_code == 200
    assert "S16P2" in response.text, "id scheme should be documented"


async def test_openapi_explorer_moved_aside(client: httpx.AsyncClient) -> None:
    """/docs is the human page, so the generated explorer lives elsewhere."""
    assert (await client.get("/api-explorer")).status_code == 200


async def test_map_page_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/map")
    assert response.status_code == 200
    assert "/static/vendor/leaflet.js" in response.text


async def test_leaflet_is_served_locally(client: httpx.AsyncClient) -> None:
    """Vendored rather than pulled from a CDN, so the page works on a locked
    down network and does not leak visitors to a third party."""
    response = await client.get("/static/vendor/leaflet.js")
    assert response.status_code == 200
    assert "Leaflet" in response.text[:200]


async def test_coverage_is_503_before_the_feed_exists(client: httpx.AsyncClient) -> None:
    assert (await client.get("/coverage.geojson")).status_code == 503


async def test_coverage_is_cached_between_requests(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Re-simplifying 114,000 points on every page load would be wasteful."""
    _write_minimal_zip(tmp_path / "gtfs.zip")

    first = await client.get("/coverage.geojson")
    assert first.status_code == 200
    assert first.headers["content-type"] == "application/geo+json"

    second = await client.get("/coverage.geojson", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304


# --- feed version -----------------------------------------------------------


def test_feed_version_is_read_back_from_the_archive(tmp_path: Path) -> None:
    path = tmp_path / "gtfs.zip"
    _write_minimal_zip(path, version="20260807-deadbeef")
    assert read_feed_version(path) == "20260807-deadbeef"


def test_feed_version_of_a_broken_archive_is_none(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not a zip")
    assert read_feed_version(path) is None
