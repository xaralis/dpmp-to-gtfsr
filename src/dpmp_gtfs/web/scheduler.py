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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.archive import read_tables
from dpmp_gtfs.config import Settings
from dpmp_gtfs.exceptions import FeedBuildError
from dpmp_gtfs.realtime.feed import build_feed_message
from dpmp_gtfs.realtime.index import StaticIndex
from dpmp_gtfs.realtime.view import VehicleView, build_vehicle_views
from dpmp_gtfs.static.builder import build_feed, iter_missing_stop_references, with_shapes
from dpmp_gtfs.static.crawler import crawl
from dpmp_gtfs.static.service_watch import load_unserved, state_path
from dpmp_gtfs.static.writer import write_feed
from dpmp_gtfs.timeutil import PRAGUE

logger = logging.getLogger(__name__)


class Scheduler:
    """Owns both refresh loops and the state they produce."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = FeedState()
        self._tasks: list[asyncio.Task[None]] = []
        self._build_lock = asyncio.Lock()
        """Guards against two rebuilds interleaving on ``self.state``.

        The initial build and ``_static_loop``'s nightly one are now created
        in the same batch (see :meth:`start`), so a cold start shortly before
        ``static_rebuild_hour`` can no longer rely on the nightly loop's
        first sleep having been computed after the initial build finished --
        both can legitimately want to run at once. Two concurrent crawls
        would both write ``gtfs.zip``; the write itself is atomic, but which
        one's result survives is then a race, and one build's success could
        be clobbered by the other's later failure."""

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Load whatever is on disk, then begin refreshing.

        The initial build, when nothing usable is on disk, runs as a
        background task rather than being awaited here. Awaiting it would
        block the ASGI lifespan itself -- and with it every route, including
        ``/healthz`` -- for as long as a cold-start crawl of ~2,700 trips
        takes. Coming up immediately instead costs nothing new: a request
        arriving before the build finishes is exactly the "not ready yet"
        state ``/vehicles.json`` and ``/healthz`` already model, not a new
        one to invent.
        """
        self._load_static_from_disk()

        self._tasks = [
            asyncio.create_task(self._realtime_loop(), name="realtime"),
            asyncio.create_task(self._static_loop(), name="static"),
        ]
        if self.state.index is None:
            logger.info("no usable static feed on disk, building one now")
            self._tasks.append(asyncio.create_task(self.rebuild_static(), name="initial-build"))

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

        # Restore which stops were out of service, so the status page is
        # accurate before the first rebuild of this process.
        previous = load_unserved(state_path(self.settings.data_dir))
        if previous is not None:
            self.state.unserved_stops = previous.stops

        logger.info(
            "loaded static feed from disk: %d trips, version %s",
            len(index),
            self.state.static_version,
        )

    def _phase(self, message: str) -> None:
        """Announce a build phase to both the log and the status endpoint.

        One string for both consumers on purpose -- an operator watching the
        log and a user watching the map must never be told two different
        things about the same build.
        """
        self.state.static_phase = message
        logger.info("static build: %s", message)

    async def rebuild_static(self) -> None:
        """Crawl, build and publish a fresh static feed.

        Skips rather than queues when a build is already running: the
        initial build and the nightly one are scheduled independently and
        can now legitimately overlap (see ``_build_lock``'s docstring), and
        a rebuild that starts the instant another finishes would just redo
        the same crawl for nothing. The check-then-acquire below has no
        ``await`` between the two, so nothing can slip in between them.
        """
        if self._build_lock.locked():
            logger.info("skipping static rebuild: one is already in progress")
            return

        async with self._build_lock:
            try:
                self._phase("stahuji jízdní řády")
                async with DpmpApiClient(self.settings) as api:
                    timetable = await crawl(api)
                feed = build_feed(timetable)

                if self.settings.shapes_enabled:
                    # Routing reaches the network, so it runs in a worker thread
                    # to keep the realtime loop ticking through a slow first
                    # build.
                    self._phase("počítám trasy")
                    feed = await asyncio.to_thread(
                        with_shapes, feed, self.settings.data_dir / "shape-cache.json"
                    )

                missing = sorted(set(iter_missing_stop_references(feed)))
                if missing:
                    # Publishing a feed with dangling stop references would
                    # break every consumer; keeping the previous one is
                    # strictly better.
                    raise FeedBuildError(
                        f"{len(missing)} stop ids referenced but not defined: {missing[:5]}"
                    )

                destination = self.settings.gtfs_zip_path
                version = write_feed(feed, destination)

                index = StaticIndex.from_zip(destination)
                self.state.index = index
                self.state.static_version = version
                self.state.static_built_at = dt.datetime.now(dt.UTC)
                self.state.trip_count = len(index)
                self.state.unserved_stops = feed.unserved_stops
                self.state.static_error = None
            except Exception as exc:
                # The previous feed stays in place and keeps being served.
                logger.exception("static rebuild failed")
                self.state.static_error = repr(exc)
            finally:
                # Whether this succeeded, failed, or is about to be retried
                # tonight, nothing is being built right now.
                self.state.static_phase = None

    async def _static_loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_rebuild())
            logger.info("starting scheduled static rebuild")
            await self.rebuild_static()

    def _seconds_until_rebuild(self) -> float:
        """Seconds until the next nightly rebuild, in local time."""
        now = dt.datetime.now(PRAGUE)
        target = now.replace(
            hour=self.settings.static_rebuild_hour, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += dt.timedelta(days=1)
        return (target - now).total_seconds()

    # -- realtime ------------------------------------------------------------

    async def refresh_realtime(self, api: DpmpApiClient) -> None:
        if self.state.index is None:
            return

        snapshot = await api.vehicles()
        message = build_feed_message(snapshot.vehicles, self.state.index, now=snapshot.time)
        vehicles = build_vehicle_views(snapshot.vehicles, self.state.index, now=snapshot.time)

        self.state.vehicles = vehicles
        self.state.realtime_message = message
        self.state.realtime = message.SerializeToString()
        self.state.realtime_built_at = dt.datetime.now(dt.UTC)
        self.state.vehicle_count = len(snapshot.vehicles)
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
    static_phase: str | None = None
    """What the static build is doing right now, or ``None`` when idle.

    Read by two consumers that must not drift apart: the log an operator
    watches and the message the map shows. A cold start is several minutes of
    discovering and downloading trips over the network, and without this the
    service looks hung.
    """
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


def read_feed_version(path: Path) -> str | None:
    """Pull ``feed_version`` out of a built archive's feed_info.txt.

    Lets a feed loaded from disk report the same version it was published
    with, rather than looking unversioned until the next rebuild.
    """
    try:
        rows = read_tables(path, "feed_info.txt")["feed_info.txt"]
    except OSError, zipfile.BadZipFile:
        return None
    return rows[0].get("feed_version") if rows else None
