"""Turns a crawled :class:`Timetable` into GTFS records."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from dpmp_gtfs.api.models import ConnectionDetail, ConnectionStop
from dpmp_gtfs.ids import route_id, station_id, stop_id, trip_id

from .calendar import LOW_FLOOR, STEP_FREE_STOP, STOP_ON_REQUEST, Service, calendar_exceptions
from .crawler import Timetable
from .overrides import STATION_COORDINATES, STATION_NAMES, unused_overrides

logger = logging.getLogger(__name__)

DAY = 24 * 3600

# DPMP runs trolleybuses on these lines and buses on the rest. The split comes
# from the CIS registry, where the trolleybus lines (655001-655033) are
# published in the separate "draha/mestske" archive. It is baked in as a
# constant rather than fetched: it changes at most once every few years, and a
# 52 MB download to learn thirteen numbers is a poor trade.
TROLLEYBUS_LINES = frozenset({1, 2, 3, 4, 5, 7, 11, 12, 13, 17, 27, 30, 33})

ROUTE_TYPE_BUS = 3
ROUTE_TYPE_TROLLEYBUS = 11

# GTFS pickup_type / drop_off_type
REGULAR = 0
COORDINATE_WITH_DRIVER = 3


def format_gtfs_time(seconds: int) -> str:
    """``87780`` -> ``"24:23:00"``.

    GTFS counts from noon-minus-twelve-hours of the *service* day, so a trip
    that starts before midnight and ends after it keeps counting past 24:00
    rather than wrapping to 00:23.
    """
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def stop_seconds(stops: list[ConnectionStop]) -> list[int]:
    """Seconds-from-service-day-start for each stop, unwrapped across midnight.

    Only three trips in the network need this (line 3 trips 322/324 at
    23:55->00:23, and line 98 trip 9 at 23:58->00:52), but getting it wrong
    turns them into trips that run backwards in time.
    """
    out: list[int] = []
    offset = 0
    previous: int | None = None

    for stop in stops:
        t = stop.time
        raw = t.hour * 3600 + t.minute * 60 + t.second
        if previous is not None and raw < previous:
            offset += DAY
        previous = raw
        out.append(raw + offset)

    return out


# --- GTFS records -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stop:
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    location_type: int
    parent_station: str
    platform_code: str
    wheelchair_boarding: int


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    route_short_name: str
    route_long_name: str
    route_type: int


@dataclass(frozen=True, slots=True)
class Trip:
    route_id: str
    service_id: str
    trip_id: str
    trip_headsign: str
    direction_id: int
    wheelchair_accessible: int


@dataclass(frozen=True, slots=True)
class StopTime:
    trip_id: str
    arrival_time: str
    departure_time: str
    stop_id: str
    stop_sequence: int
    pickup_type: int
    drop_off_type: int


@dataclass(slots=True)
class Feed:
    stops: list[Stop] = field(default_factory=list)
    unserved_stops: dict[str, str] = field(default_factory=dict)
    """Stop id -> name for stops excluded from the feed because nothing calls
    at them. Kept so rebuilds can spot diversions starting and ending."""
    routes: list[Route] = field(default_factory=list)
    trips: list[Trip] = field(default_factory=list)
    stop_times: list[StopTime] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    start_date: dt.date = field(default_factory=dt.date.today)
    end_date: dt.date = field(default_factory=dt.date.today)

    @property
    def calendar_exceptions(self) -> list[object]:
        return list(calendar_exceptions(self.services, self.start_date, self.end_date))


# --- building ---------------------------------------------------------------


def used_platforms(timetable: Timetable) -> dict[int, set[int]]:
    """``{station: {platform, ...}}`` as actually referenced by timetables.

    The authority on which platforms exist is the timetable, not
    ``/api/stations`` -- the latter is missing several that trips call at.
    """
    used: dict[int, set[int]] = {}
    for detail in timetable.details.values():
        for stop in detail.stops:
            used.setdefault(stop.number, set()).add(stop.platform)
    return used


def build_stops(timetable: Timetable) -> list[Stop]:
    """One parent station per stop plus one child per platform.

    Timetables and realtime both address platforms, so the children carry the
    actual service; the parent exists so that consumers can group them and so
    that transfers between platforms are understood.

    Platforms the API omits are backfilled -- see
    :mod:`dpmp_gtfs.static.overrides` for why that is necessary and where the
    coordinates come from.
    """
    stops: list[Stop] = []
    used = used_platforms(timetable)
    known = {s.number for s in timetable.stations}

    if redundant := unused_overrides(known):
        logger.info(
            "coordinate overrides for stations %s are no longer needed; "
            "the API now provides them and the table can be trimmed",
            sorted(redundant),
        )

    for station in timetable.stations:
        step_free = int(STEP_FREE_STOP in station.codes)
        lat, lon = station.gps_latitude, station.gps_longitude

        if lat is None or lon is None:
            if not station.platforms:
                logger.warning("station %s (%s) has no coordinates", station.number, station.name)
                continue
            lat = sum(p.gps_latitude for p in station.platforms) / len(station.platforms)
            lon = sum(p.gps_longitude for p in station.platforms) / len(station.platforms)

        parent = station_id(station.number)
        stops.append(
            Stop(
                stop_id=parent,
                stop_name=station.name,
                stop_lat=lat,
                stop_lon=lon,
                location_type=1,
                parent_station="",
                platform_code="",
                wheelchair_boarding=step_free,
            )
        )
        placed = {p.number for p in station.platforms}
        for platform in station.platforms:
            stops.append(
                Stop(
                    stop_id=stop_id(station.number, platform.number),
                    stop_name=station.name,
                    stop_lat=platform.gps_latitude,
                    stop_lon=platform.gps_longitude,
                    location_type=0,
                    parent_station=parent,
                    platform_code=str(platform.number),
                    wheelchair_boarding=step_free,
                )
            )

        # Platforms that timetables call at but /api/stations omits. The
        # station's own position is the best available answer and is good to a
        # few tens of metres.
        for platform_number in sorted(used.get(station.number, set()) - placed):
            logger.info(
                "platform %s/%s missing from API, placing it at the station",
                station.number,
                platform_number,
            )
            stops.append(
                Stop(
                    stop_id=stop_id(station.number, platform_number),
                    stop_name=station.name,
                    stop_lat=lat,
                    stop_lon=lon,
                    location_type=0,
                    parent_station=parent,
                    platform_code=str(platform_number),
                    wheelchair_boarding=step_free,
                )
            )

    # Stations the API does not list at all.
    for number in sorted(set(used) - known):
        coords = STATION_COORDINATES.get(number)
        if coords is None:
            logger.error(
                "station %s is used by timetables but is unknown to the API and "
                "has no coordinate override; its trips will be rejected",
                number,
            )
            continue

        name = STATION_NAMES.get(number, f"Zastávka {number}")
        parent = station_id(number)
        logger.info("station %s (%s) absent from API, using %s", number, name, coords.source)
        stops.append(
            Stop(
                stop_id=parent,
                stop_name=name,
                stop_lat=coords.latitude,
                stop_lon=coords.longitude,
                location_type=1,
                parent_station="",
                platform_code="",
                wheelchair_boarding=0,
            )
        )
        for platform_number in sorted(used[number]):
            stops.append(
                Stop(
                    stop_id=stop_id(number, platform_number),
                    stop_name=name,
                    stop_lat=coords.latitude,
                    stop_lon=coords.longitude,
                    location_type=0,
                    parent_station=parent,
                    platform_code=str(platform_number),
                    wheelchair_boarding=0,
                )
            )

    return stops


def build_routes(timetable: Timetable) -> list[Route]:
    routes: list[Route] = []
    for line in timetable.lines:
        terminals = ""
        if len(line.stops) >= 2:
            terminals = f"{line.stops[0].name} - {line.stops[-1].name}"
        routes.append(
            Route(
                route_id=route_id(line.number),
                route_short_name=str(line.number),
                route_long_name=terminals,
                route_type=(
                    ROUTE_TYPE_TROLLEYBUS if line.number in TROLLEYBUS_LINES else ROUTE_TYPE_BUS
                ),
            )
        )
    return routes


def direction_of(detail: ConnectionDetail) -> int:
    """0 or 1, from whether the trip walks the line's stop list up or down.

    ``index`` is each stop's position in the line's canonical ordering, so a
    trip in one direction sees it ascend and the other sees it descend.
    """
    indices = [s.index for s in detail.stops]
    if len(indices) < 2:
        return 0
    return 0 if indices[-1] > indices[0] else 1


def build_trips_and_stop_times(
    timetable: Timetable,
) -> tuple[list[Trip], list[StopTime], list[Service]]:
    trips: list[Trip] = []
    stop_times: list[StopTime] = []
    services: dict[str, Service] = {}

    for key, detail in sorted(timetable.details.items()):
        line_number, connection_number = key
        summary = timetable.summaries[key]
        if not detail.stops:
            logger.warning("trip %s/%s has no stops, skipping", line_number, connection_number)
            continue

        service = Service.from_codes(summary.codes)
        services.setdefault(service.service_id, service)

        tid = trip_id(line_number, connection_number)
        trips.append(
            Trip(
                route_id=route_id(line_number),
                service_id=service.service_id,
                trip_id=tid,
                trip_headsign=detail.stops[-1].name,
                direction_id=direction_of(detail),
                # Code 3 ("guaranteed low-floor") is present on every trip in
                # the network, but it is read rather than assumed.
                wheelchair_accessible=1 if LOW_FLOOR in summary.codes else 0,
            )
        )

        for sequence, (stop, seconds) in enumerate(
            zip(detail.stops, stop_seconds(detail.stops), strict=True)
        ):
            # "Zastávka na znamení" -- the bus only calls if asked.
            on_request = STOP_ON_REQUEST in stop.codes
            boarding = COORDINATE_WITH_DRIVER if on_request else REGULAR
            time = format_gtfs_time(seconds)
            stop_times.append(
                StopTime(
                    trip_id=tid,
                    arrival_time=time,
                    departure_time=time,
                    stop_id=stop_id(stop.number, stop.platform),
                    stop_sequence=sequence,
                    pickup_type=boarding,
                    drop_off_type=boarding,
                )
            )

    return trips, stop_times, sorted(services.values(), key=lambda s: s.service_id)


def prune_unserved_stops(
    stops: list[Stop], stop_times: list[StopTime]
) -> tuple[list[Stop], dict[str, str]]:
    """Split stops into those with service and those without.

    DPMP's stop register keeps entries nothing calls at -- Třída Míru is a
    pedestrian zone, and interchanges such as Hlavní nádraží list spare
    platforms. Publishing them would put stops on the map that no vehicle ever
    reaches, which is worse than omitting them.

    The dropped ones are returned rather than discarded: losing service is not
    always permanent. A diversion takes stops out temporarily, so the caller
    compares this set across rebuilds (see :mod:`dpmp_gtfs.static.watch`).
    """
    served = {st.stop_id for st in stop_times}
    platforms = [s for s in stops if s.location_type == 0 and s.stop_id in served]
    live_parents = {s.parent_station for s in platforms}

    kept = [s for s in stops if s.location_type == 1 and s.stop_id in live_parents]
    kept.extend(platforms)

    kept_ids = {s.stop_id for s in kept}
    unserved = {s.stop_id: s.stop_name for s in stops if s.stop_id not in kept_ids}
    if unserved:
        logger.info("%d stops have no scheduled service", len(unserved))

    ordered = sorted(
        kept, key=lambda s: (s.parent_station or s.stop_id, s.location_type, s.stop_id)
    )
    return ordered, unserved


def build_feed(
    timetable: Timetable,
    start_date: dt.date | None = None,
    validity_days: int = 365,
) -> Feed:
    """Assemble a complete feed.

    The API publishes no validity window, so one is synthesised. A daily
    rebuild keeps rolling it forward; if the service ever stops rebuilding, the
    feed expires rather than silently claiming to be current forever.
    """
    start = start_date or dt.date.today()
    trips, stop_times, services = build_trips_and_stop_times(timetable)
    stops, unserved = prune_unserved_stops(build_stops(timetable), stop_times)

    feed = Feed(
        stops=stops,
        unserved_stops=unserved,
        routes=build_routes(timetable),
        trips=trips,
        stop_times=stop_times,
        services=services,
        start_date=start,
        end_date=start + dt.timedelta(days=validity_days),
    )
    logger.info(
        "built feed: %d stops, %d routes, %d trips, %d stop times, %d services",
        len(feed.stops),
        len(feed.routes),
        len(feed.trips),
        len(feed.stop_times),
        len(feed.services),
    )
    return feed


def iter_missing_stop_references(feed: Feed) -> Iterator[str]:
    """Stop ids used by stop_times that no stop declares.

    Cheap internal consistency check, run before writing so a broken feed is
    never published.
    """
    known = {s.stop_id for s in feed.stops}
    for st in feed.stop_times:
        if st.stop_id not in known:
            yield st.stop_id
