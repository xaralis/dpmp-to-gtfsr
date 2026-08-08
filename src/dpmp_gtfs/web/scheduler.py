"""Background refresh of both feeds.

Design constraint: the upstream API goes away for minutes at a time. Neither
loop may die when that happens, and neither may publish an empty feed to cover
it up -- an empty realtime feed says "no vehicles are running", which is a
factual claim, not a graceful degradation. Instead the last good feed is kept
and its age is exposed, letting consumers decide for themselves.
"""

import asyncio
import contextlib
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.config import Settings
from dpmp_gtfs.exceptions import FeedBuildError
from dpmp_gtfs.realtime.feed import build_feed_message
from dpmp_gtfs.realtime.index import StaticIndex
from dpmp_gtfs.realtime.tracker import DelayTracker
from dpmp_gtfs.realtime.view import VehicleView, build_vehicle_views
from dpmp_gtfs.static.builder import build_feed, iter_missing_stop_references, with_shapes
from dpmp_gtfs.static.crawler import crawl
from dpmp_gtfs.static.watch import load_unserved as load_unserved
from dpmp_gtfs.static.watch import write_unserved
from dpmp_gtfs.static.writer import write_feed

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FeedState:
    """What the service currently has to serve, and how healthy it is."""

    realtime: bytes = b""
    realtime_message: rt.FeedMessage | None = None
    realtime_built_at: dt.datetime | None = None
    realtime_error: str | None = None
    vehicle_count: int = 0
    vehicles: list[VehicleView] = field(default_factory=list)
    """Live vehicles joined against the timetable, ready to display. Built
    once per refresh rather than per request -- every consumer wants the same
    answer, and the join is not free."""

    static_version: str | None = None
    static_built_at: dt.datetime | None = None
    static_error: str | None = None
    trip_count: int = 0
    unserved_stops: dict[str, str] = field(default_factory=dict)

    index: StaticIndex | None = None

    def age(self, moment: dt.datetime | None = None) -> float | None:
        """Seconds since the realtime feed was last rebuilt."""
        if self.realtime_built_at is None:
            return None
        now = moment or dt.datetime.now(dt.UTC)
        return (now - self.realtime_built_at).total_seconds()

    @property
    def healthy(self) -> bool:
        """Whether the service is currently serving trustworthy data.

        Deliberately strict about staleness: a realtime feed several minutes
        old is worse than an honest failure, because consumers will present it
        as current.

        Health depends on having a usable static feed, not on having built one
        in this process -- a feed loaded from disk at startup is just as valid,
        and requiring otherwise would report an unhealthy service for the whole
        first day.
        """
        age = self.age()
        return self.index is not None and age is not None and age < 120


class Scheduler:
    """Owns both refresh loops and the state they produce."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = FeedState()
        self._tracker: DelayTracker | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Load whatever is on disk, then begin refreshing.

        Reusing an existing gtfs.zip matters: a cold start would otherwise
        crawl 2,700 trips before the service could answer anything.
        """
        self._load_static_from_disk()
        if self.state.index is None:
            logger.info("no usable static feed on disk, building one now")
            await self.rebuild_static()

        self._tasks = [
            asyncio.create_task(self._realtime_loop(), name="realtime"),
            asyncio.create_task(self._static_loop(), name="static"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    # -- static --------------------------------------------------------------

    def _load_static_from_disk(self) -> None:
        path = self.settings.gtfs_zip_path
        if not path.exists():
            return
        try:
            index = StaticIndex.from_zip(path)
        except Exception:
            logger.exception("could not read existing static feed at %s", path)
            return

        self.state.index = index
        self.state.trip_count = len(index)
        self.state.static_built_at = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
        self.state.static_version = read_feed_version(path)
        self._tracker = DelayTracker(index)

        # Restore which stops were out of service, so the status page is
        # accurate before the first rebuild of this process.
        previous = load_unserved(path.parent / "unserved-stops.json")
        if previous is not None:
            self.state.unserved_stops = previous.stops

        logger.info(
            "loaded static feed from disk: %d trips, version %s",
            len(index),
            self.state.static_version,
        )

    async def rebuild_static(self) -> None:
        try:
            async with DpmpApiClient(self.settings) as api:
                timetable = await crawl(api)
            feed = build_feed(timetable)

            if self.settings.shapes_enabled:
                # Routing reaches the network, so it runs in a worker thread to
                # keep the realtime loop ticking through a slow first build.
                feed = await asyncio.to_thread(
                    with_shapes, feed, self.settings.data_dir / "shape-cache.json"
                )

            missing = sorted(set(iter_missing_stop_references(feed)))
            if missing:
                # Publishing a feed with dangling stop references would break
                # every consumer; keeping the previous one is strictly better.
                raise FeedBuildError(
                    f"{len(missing)} stop ids referenced but not defined: {missing[:5]}"
                )

            destination = self.settings.gtfs_zip_path
            version = write_feed(feed, destination)

            write_unserved(self.settings.data_dir, feed.unserved_stops)

            index = StaticIndex.from_zip(destination)
            self.state.index = index
            self._tracker = DelayTracker(index)
            self.state.static_version = version
            self.state.static_built_at = dt.datetime.now(dt.UTC)
            self.state.trip_count = len(index)
            self.state.unserved_stops = feed.unserved_stops
            self.state.static_error = None
        except Exception as exc:
            # The previous feed stays in place and keeps being served.
            logger.exception("static rebuild failed")
            self.state.static_error = repr(exc)

    async def _static_loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_rebuild())
            logger.info("starting scheduled static rebuild")
            await self.rebuild_static()

    def _seconds_until_rebuild(self) -> float:
        """Seconds until the next nightly rebuild, in local time."""
        from zoneinfo import ZoneInfo

        prague = ZoneInfo("Europe/Prague")
        now = dt.datetime.now(prague)
        target = now.replace(
            hour=self.settings.static_rebuild_hour, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += dt.timedelta(days=1)
        return (target - now).total_seconds()

    # -- realtime ------------------------------------------------------------

    async def refresh_realtime(self, api: DpmpApiClient) -> None:
        if self.state.index is None or self._tracker is None:
            return

        buses = await api.buses()
        self._tracker.observe(buses)
        message = build_feed_message(buses, self.state.index, self._tracker)
        vehicles = build_vehicle_views(buses, self.state.index, self._tracker)

        self.state.vehicles = vehicles
        self.state.realtime_message = message
        self.state.realtime = message.SerializeToString()
        self.state.realtime_built_at = dt.datetime.now(dt.UTC)
        self.state.vehicle_count = len(buses)
        self.state.realtime_error = None

    async def _realtime_loop(self) -> None:
        async with DpmpApiClient(self.settings) as api:
            while True:
                try:
                    await self.refresh_realtime(api)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Keep serving the last good feed. Replacing it with an
                    # empty one would assert that no vehicles are running.
                    logger.warning("realtime refresh failed: %r", exc)
                    self.state.realtime_error = repr(exc)
                await asyncio.sleep(self.settings.realtime_interval)


def read_feed_version(path: Path) -> str | None:
    """Pull ``feed_version`` out of a built archive's feed_info.txt.

    Lets a feed loaded from disk report the same version it was published
    with, rather than looking unversioned until the next rebuild.
    """
    import csv
    import io
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf, zf.open("feed_info.txt") as fh:
            rows = list(csv.DictReader(io.TextIOWrapper(fh, "utf8")))
        return rows[0].get("feed_version") if rows else None
    except OSError, KeyError, zipfile.BadZipFile:
        return None
