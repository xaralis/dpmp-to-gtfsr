"""HTTP service: serves both feeds, a status page and documentation."""

import datetime as dt
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.protobuf.json_format import MessageToDict

from dpmp_gtfs.config import Settings
from dpmp_gtfs.config import settings as default_settings
from dpmp_gtfs.realtime.view import as_payload

from .coverage import build_coverage
from .scheduler import Scheduler

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def _etag(payload: bytes) -> str:
    return f'"{hashlib.sha256(payload).hexdigest()[:32]}"'


def _not_modified(request: Request, etag: str) -> Response | None:
    """A 304 when the client already holds this exact body.

    Both feed routes need this, and they need to agree on the tag format --
    a client holding an old-format tag would otherwise be served stale
    responses by one route and fresh ones by the other.
    """
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or default_settings
    scheduler = Scheduler(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    app = FastAPI(
        title="dpmp-to-gtfsr",
        description="GTFS a GTFS-Realtime feed pro pardubickou MHD.",
        # The default /docs is a human-facing page here, so the OpenAPI
        # explorer moves aside.
        docs_url="/api-explorer",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.scheduler = scheduler
    app.state.settings = config
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # -- feeds ---------------------------------------------------------------

    @app.get("/gtfs.zip", response_class=Response, tags=["feeds"])
    def gtfs_static(request: Request) -> Response:
        """The static timetable feed."""
        path = config.gtfs_zip_path
        if not path.exists():
            return Response("static feed is not built yet", status_code=503)

        payload = path.read_bytes()
        etag = _etag(payload)
        if cached := _not_modified(request, etag):
            return cached

        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
        return Response(
            payload,
            media_type="application/zip",
            headers={
                "ETag": etag,
                "Last-Modified": modified.strftime("%a, %d %b %Y %H:%M:%S GMT"),
                "Content-Disposition": 'attachment; filename="gtfs.zip"',
                # Rebuilt nightly; an hour of caching costs nothing.
                "Cache-Control": "public, max-age=3600",
            },
        )

    @app.get("/gtfs-rt.pb", response_class=Response, tags=["feeds"])
    def gtfs_realtime() -> Response:
        """Vehicle positions and trip updates, as GTFS-Realtime protobuf."""
        state = scheduler.state
        if not state.realtime:
            return Response("realtime feed is not ready yet", status_code=503)
        return Response(
            state.realtime,
            media_type="application/x-protobuf",
            headers={"Cache-Control": f"public, max-age={int(config.realtime_interval)}"},
        )

    @app.get("/gtfs-rt.json", tags=["feeds"])
    def gtfs_realtime_json() -> JSONResponse:
        """The same realtime feed as JSON. For debugging, not for consumption."""
        message = scheduler.state.realtime_message
        if message is None:
            return JSONResponse({"detail": "realtime feed is not ready yet"}, status_code=503)
        return JSONResponse(MessageToDict(message, preserving_proto_field_name=True))

    @app.get("/coverage.geojson", tags=["feeds"])
    def coverage(request: Request) -> Response:
        """Routes and stops as GeoJSON, for the map page.

        Cached against the feed on disk: rebuilding this on every page load
        would re-read and re-simplify 114,000 points for no reason.
        """
        path = config.gtfs_zip_path
        if not path.exists():
            return JSONResponse({"detail": "static feed is not built yet"}, status_code=503)

        stamp = str(path.stat().st_mtime_ns)
        cached = getattr(app.state, "coverage_cache", None)
        if cached is None or cached[0] != stamp:
            payload = json.dumps(build_coverage(path)).encode()
            app.state.coverage_cache = (stamp, payload)
        else:
            payload = cached[1]

        etag = _etag(payload)
        if not_modified := _not_modified(request, etag):
            return not_modified

        return Response(
            payload,
            media_type="application/geo+json",
            headers={"ETag": etag, "Cache-Control": "public, max-age=3600"},
        )

    @app.get("/vehicles.json", tags=["feeds"])
    def vehicles() -> JSONResponse:
        """Live vehicles with their surrounding stops and delay, resolved.

        GTFS-Realtime carries stop ids and expects the consumer to hold the
        timetable; this is the same data with those ids already turned into
        names and times, for anything that wants to show a vehicle to a person.
        Not a standard format -- use /gtfs-rt.pb for that.
        """
        state = scheduler.state
        if state.realtime_built_at is None:
            return JSONResponse({"detail": "realtime feed is not ready yet"}, status_code=503)
        return JSONResponse(
            as_payload(state.vehicles, state.realtime_built_at),
            headers={"Cache-Control": f"public, max-age={int(config.realtime_interval)}"},
        )

    # -- status --------------------------------------------------------------

    def _status() -> dict[str, Any]:
        state = scheduler.state
        return {
            "healthy": state.healthy,
            "realtime": {
                "built_at": state.realtime_built_at.isoformat()
                if state.realtime_built_at
                else None,
                "age_seconds": state.age(),
                "vehicles": state.vehicle_count,
                "error": state.realtime_error,
            },
            "static": {
                "version": state.static_version,
                "built_at": state.static_built_at.isoformat() if state.static_built_at else None,
                "trips": state.trip_count,
                "stops_without_service": len(state.unserved_stops),
                "error": state.static_error,
            },
        }

    @app.get("/healthz", tags=["status"])
    def healthz() -> JSONResponse:
        """Feed freshness and the last error from each loop."""
        payload = _status()
        return JSONResponse(payload, status_code=200 if payload["healthy"] else 503)

    # -- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def homepage(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"status": _status(), "settings": config},
        )

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    def documentation(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="docs.html",
            context={"status": _status(), "settings": config},
        )

    @app.get("/map", response_class=HTMLResponse, include_in_schema=False)
    def coverage_map(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="map.html",
            context={"status": _status(), "settings": config},
        )

    return app


app = create_app()
