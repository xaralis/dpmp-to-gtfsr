"""Command line entry points."""

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import typer

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.cis import build_calendars, fetch_archives
from dpmp_gtfs.config import settings
from dpmp_gtfs.static.builder import (
    VALIDITY_DAYS,
    build_feed,
    iter_missing_stop_references,
    with_shapes,
)
from dpmp_gtfs.static.crawler import crawl
from dpmp_gtfs.static.discovery import discover_trips
from dpmp_gtfs.static.writer import write_feed

app = typer.Typer(help="GTFS / GTFS-Realtime feed tooling for Pardubice public transport.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("dpmp_gtfs")


@app.command("build-static")
def build_static(
    dest: Path = typer.Option(None, help="Output path. Defaults to <data_dir>/gtfs.zip."),
    shapes: bool = typer.Option(True, help="Route trip geometry against OpenStreetMap."),
) -> None:
    """Crawl the full timetable and write a GTFS zip."""

    async def run() -> None:
        today = dt.date.today()
        archives = await fetch_archives(settings.cis_urls, settings.cis_dir)
        calendars = build_calendars(archives, today, today + dt.timedelta(days=VALIDITY_DAYS))

        async with DpmpApiClient() as api:
            timetable = await crawl(api, calendars)

        destination = dest or settings.gtfs_zip_path
        feed = build_feed(timetable, start_date=today)

        if shapes:
            feed = with_shapes(feed, destination.parent / "shape-cache.json")

        if missing := sorted(set(iter_missing_stop_references(feed))):
            # A dangling stop reference makes the whole feed invalid, so refuse
            # rather than publish something consumers will reject.
            typer.echo(
                f"Aborting: {len(missing)} stop ids referenced but not defined: "
                f"{', '.join(missing[:10])}",
                err=True,
            )
            raise typer.Exit(1)

        write_feed(feed, destination)

    asyncio.run(run())


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", help="Address to bind."),
    port: int = typer.Option(8000, help="Port to bind."),
    reload: bool = typer.Option(False, help="Reload on code changes (development only)."),
) -> None:
    """Run the HTTP service that publishes both feeds."""
    import uvicorn

    uvicorn.run("dpmp_gtfs.web.app:app", host=host, port=port, reload=reload)


@app.command("dump-fixtures")
def dump_fixtures(
    dest: Path = typer.Option(Path("tests/fixtures"), help="Where to write the recordings."),
    lines: int = typer.Option(2, help="How many lines to record connection details for."),
) -> None:
    """Record live API responses so tests can run against real data offline."""

    async def run() -> None:
        async with DpmpApiClient() as api:
            _write(dest / "stops.json", [s.model_dump(mode="json") for s in await api.stops()])

            line_list = await api.lines()
            _write(dest / "lines.json", [line.model_dump(mode="json") for line in line_list])

            for line in line_list[:lines]:
                connections = await discover_trips(api, line.id)
                # A couple of trips is enough to exercise the stop_times path.
                for number in sorted(connections)[:3]:
                    _write(
                        dest / f"connection-{line.id}-{number}.json",
                        connections[number].model_dump(mode="json"),
                    )

            _write(dest / "vehicles.json", (await api.vehicles()).model_dump(mode="json"))

    asyncio.run(run())


@app.command("watch-vehicles")
def watch_vehicles(
    dest: Path = typer.Option(Path("tests/fixtures/snapshots"), help="Where to write snapshots."),
    interval: float = typer.Option(15.0, help="Seconds between snapshots."),
    count: int = typer.Option(60, help="How many snapshots to record."),
) -> None:
    """Record a timed sequence of /vehicles snapshots, for manual inspection.

    The delay tracker this used to feed is gone -- ``currentDelay`` is a real
    delay straight from the upstream now, not something reconstructed from
    watching a vehicle move between stops -- but a timed sequence of snapshots
    is still handy for debugging the live feed by hand.
    """

    async def run() -> None:
        dest.mkdir(parents=True, exist_ok=True)

        async with DpmpApiClient() as api:
            for i in range(count):
                stamp = dt.datetime.now(dt.UTC)
                try:
                    snapshot = await api.vehicles()
                except Exception:
                    logger.exception("snapshot %d failed, continuing", i)
                else:
                    name = stamp.strftime("%Y%m%dT%H%M%SZ")
                    _write(
                        dest / f"vehicles-{name}.json",
                        {
                            "recorded_at": stamp.isoformat(),
                            **snapshot.model_dump(mode="json"),
                        },
                    )

                if i + 1 < count:
                    await asyncio.sleep(interval)

    asyncio.run(run())


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf8")
    logger.info("wrote %s (%d B)", path, path.stat().st_size)


if __name__ == "__main__":
    app()
