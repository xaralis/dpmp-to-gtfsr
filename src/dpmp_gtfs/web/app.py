"""HTTP service: serves both feeds, a status page and documentation."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google.protobuf.json_format import MessageToDict

from dpmp_gtfs.config import Settings
from dpmp_gtfs.config import settings as default_settings

from .scheduler import Scheduler

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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

    # -- feeds ---------------------------------------------------------------

    @app.get("/gtfs.zip", response_class=Response, tags=["feeds"])
    def gtfs_static(request: Request) -> Response:
        """The static timetable feed."""
        path = config.gtfs_zip_path
        if not path.exists():
            return Response("static feed is not built yet", status_code=503)

        payload = path.read_bytes()
        etag = f'"{hashlib.sha256(payload).hexdigest()[:32]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})

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

    return app


app = create_app()
