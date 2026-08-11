"""Turns a crawled :class:`Timetable` into GTFS records."""

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from dpmp_gtfs.api.models import Connection, ConnectionStop
from dpmp_gtfs.exceptions import FeedBuildError
from dpmp_gtfs.ids import route_id, station_id, stop_id, trip_id
from dpmp_gtfs.timeutil import DAY, format_gtfs_time
from dpmp_gtfs.types import (
    Feed,
    LatLon,
    Route,
    Service,
    Stop,
    StopSequence,
    StopTime,
    Timetable,
    Trip,
    TripGeometry,
)
from dpmp_gtfs.upstream import TROLLEYBUS_LINES

from .calendar import calendar_exceptions, service_from_codes, service_from_dates
from .shapes import ShapeCache, ValhallaRouter, build_shapes

logger = logging.getLogger(__name__)

VALIDITY_DAYS = 365
"""How far ahead a built feed claims to be valid.

Shared with whoever fetches the CIS calendars, because that is the window they
have to cover: a horizon one day short leaves every service looking cancelled
on the feed's last day."""

MAX_FALLBACK_SHARE = 0.10
"""Above this share of trips missing from CIS, the two sources have drifted far
enough apart that the feed stops being trustworthy. Measured at 2.3 %."""

ROUTE_TYPE_BUS = 3
ROUTE_TYPE_TROLLEYBUS = 11

# GTFS pickup_type / drop_off_type
REGULAR = 0
COORDINATE_WITH_DRIVER = 3


def build_feed(
    timetable: Timetable,
    start_date: dt.date | None = None,
    validity_days: int = VALIDITY_DAYS,
) -> Feed:
    """Assemble a complete feed.

    The API publishes no validity window, so one is synthesised. A daily
    rebuild keeps rolling it forward; if the service ever stops rebuilding, the
    feed expires rather than silently claiming to be current forever.
    """
    start = start_date or dt.date.today()
    end = start + dt.timedelta(days=validity_days)
    trips, stop_times, services = build_trips_and_stop_times(timetable, start, end)
    stops, unserved = prune_unserved_stops(build_stops(timetable), stop_times)

    feed = Feed(
        stops=stops,
        unserved_stops=unserved,
        routes=build_routes(timetable),
        trips=trips,
        stop_times=stop_times,
        services=services,
        start_date=start,
        end_date=end,
    )
    feed.calendar_exceptions = list(
        calendar_exceptions(feed.services, feed.start_date, feed.end_date)
    )
    logger.info(
        "built feed: %d stops, %d routes, %d trips, %d stop times, %d services, %d shapes",
        len(feed.stops),
        len(feed.routes),
        len(feed.trips),
        len(feed.stop_times),
        len(feed.services),
        len(feed.shapes),
    )
    return feed


def with_shapes(feed: Feed, cache_path: Path, router: ValhallaRouter | None = None) -> Feed:
    """Route geometry for a built feed and attach it.

    Separate from :func:`build_feed` because routing reaches the network and
    can fail: a feed without geometry is still a good feed, so this returns
    the original unchanged rather than raising.
    """
    sequences = stop_sequences(feed.stop_times, feed.stops)
    if not sequences:
        return feed

    shapes = build_shapes(sequences, ShapeCache(cache_path), router)
    if not shapes:
        logger.warning("no geometry available; publishing the feed without shapes.txt")
        return feed

    trips, stop_times = apply_shapes(feed.trips, feed.stop_times, shapes)
    attached = {trip.shape_id for trip in trips if trip.shape_id}
    used = sorted((s for s in shapes.values() if s.shape_id in attached), key=lambda s: s.shape_id)

    logger.info(
        "attached %d shapes to %d of %d trips",
        len(used),
        sum(1 for t in trips if t.shape_id),
        len(trips),
    )
    return replace(feed, trips=trips, stop_times=stop_times, shapes=used)


def build_stops(timetable: Timetable) -> list[Stop]:
    """One parent station per stop plus one child per platform in service.

    Timetables and realtime both address platforms, so the children carry the
    actual service; the parent exists so consumers can group them and so that
    transfers between platforms are understood.

    Platforms inherit the parent's position: no upstream publishes per-platform
    coordinates any more (see :mod:`dpmp_gtfs.upstream`). Their numbers are
    real, so the ids and ``platform_code`` are unchanged -- only the geometry
    is coarser than it used to be.

    A handful of stops (e.g. 147, "Opočínek,rozvodna") publish no coordinates at
    all; those are skipped rather than published with a nonsensical position.
    :func:`build_trips_and_stop_times` applies the same exclusion so
    ``stop_times.txt`` never points at a stop this function dropped.
    """
    stops: list[Stop] = []
    used = used_platforms(timetable)
    known_ids = {s.id for s in timetable.stops}

    for api_stop in timetable.stops:
        if api_stop.gps_latitude is None or api_stop.gps_longitude is None:
            logger.warning("stop %s (%s) has no coordinates, skipping", api_stop.id, api_stop.name)
            continue

        step_free = int(api_stop.step_free)
        parent = station_id(api_stop.id)
        stops.append(
            Stop(
                stop_id=parent,
                stop_name=api_stop.name,
                stop_lat=api_stop.gps_latitude,
                stop_lon=api_stop.gps_longitude,
                location_type=1,
                parent_station="",
                platform_code="",
                wheelchair_boarding=step_free,
            )
        )
        for platform in sorted(used.get(api_stop.id, set())):
            stops.append(
                Stop(
                    stop_id=stop_id(api_stop.id, platform),
                    stop_name=api_stop.name,
                    stop_lat=api_stop.gps_latitude,
                    stop_lon=api_stop.gps_longitude,
                    location_type=0,
                    parent_station=parent,
                    platform_code=str(platform),
                    wheelchair_boarding=step_free,
                )
            )

    for number in sorted(set(used) - known_ids):
        logger.warning(
            "stop %s is used by timetables but is unknown to /stops; the calls "
            "at it are dropped and the trips keep the rest of their route",
            number,
        )

    return stops


def unplaceable_stops(timetable: Timetable) -> set[int]:
    """Ids no ``stops.txt`` row will describe.

    Two ways a stop lands here: ``/stops`` publishes it without a position, or
    a timetable calls at it and ``/stops`` does not publish it at all. Both are
    upstream defects and both leave the same hole, so they are one set.

    Shared between :func:`build_stops`, which leaves them out of ``stops.txt``,
    and :func:`build_trips_and_stop_times`, which drops the matching
    ``stop_times.txt`` rows. Skipping the second half is what turns one bad
    stop into a feed nobody can publish: the rows dangle,
    :func:`iter_missing_stop_references` finds them, and every rebuild refuses
    for as long as upstream leaves it that way.
    """
    known = {s.id for s in timetable.stops}
    placed = {
        s.id for s in timetable.stops if s.gps_latitude is not None and s.gps_longitude is not None
    }
    called_at = {
        stop.stop_id for connection in timetable.connections.values() for stop in connection.stops
    }
    return (known - placed) | (called_at - known)


def build_routes(timetable: Timetable) -> list[Route]:
    """Routes, with terminals taken from each line's longest trip.

    ``/lines`` returns only ``{id, jdfId, enabled}`` -- the stop list the old
    API published alongside it is gone -- so the longest trip stands in for the
    line's shape. It is already fetched, so this costs nothing.
    """
    names = {s.id: s.name for s in timetable.stops}
    longest: dict[str, Connection] = {}
    for (line_id, _), connection in timetable.connections.items():
        current = longest.get(line_id)
        if current is None or len(connection.stops) > len(current.stops):
            longest[line_id] = connection

    routes: list[Route] = []
    for line in timetable.lines:
        terminals = ""
        if (best := longest.get(line.id)) and len(best.stops) >= 2:
            first = names.get(best.stops[0].stop_id, "")
            last = names.get(best.stops[-1].stop_id, "")
            terminals = f"{first} - {last}"
        number = int(line.id) if line.id.isdigit() else 0
        routes.append(
            Route(
                route_id=route_id(line.id),
                route_short_name=line.id,
                route_long_name=terminals,
                route_type=(
                    ROUTE_TYPE_TROLLEYBUS if number in TROLLEYBUS_LINES else ROUTE_TYPE_BUS
                ),
            )
        )
    return routes


def build_trips_and_stop_times(
    timetable: Timetable, start: dt.date, end: dt.date
) -> tuple[list[Trip], list[StopTime], list[Service]]:
    trips: list[Trip] = []
    stop_times: list[StopTime] = []
    services: dict[str, Service] = {}
    names = {s.id: s.name for s in timetable.stops}
    on_request_stops = {s.id for s in timetable.stops if s.on_request}
    unplaceable = unplaceable_stops(timetable)
    jdf_ids = {line.id: line.jdf_id for line in timetable.lines}
    from_cis = 0
    from_codes = 0
    no_weekly_pattern = 0

    for key, connection in sorted(timetable.connections.items()):
        line_id, connection_number = key
        if not connection.stops:
            logger.warning("trip %s/%s has no stops, skipping", line_id, connection_number)
            continue

        tid = trip_id(line_id, connection_number)
        trip_stop_times: list[StopTime] = []
        surviving_stops: list[ConnectionStop] = []

        for sequence, (stop, seconds) in enumerate(
            zip(connection.stops, stop_seconds(connection.stops), strict=True)
        ):
            if stop.stop_id in unplaceable:
                # Not in stops.txt (build_stops already logged this stop); a
                # stop_times row here would dangle.
                continue
            if not stop.platform_id.isdigit():
                logger.warning(
                    "trip %s stop %s has no numeric platform (%r), skipping the stop",
                    tid,
                    stop.stop_id,
                    stop.platform_id,
                )
                continue

            # "Zastávka na znamení" -- the bus only calls if asked.
            on_request = stop.stop_id in on_request_stops
            boarding = COORDINATE_WITH_DRIVER if on_request else REGULAR
            time = format_gtfs_time(seconds)
            trip_stop_times.append(
                StopTime(
                    trip_id=tid,
                    arrival_time=time,
                    departure_time=time,
                    stop_id=stop_id(stop.stop_id, int(stop.platform_id)),
                    stop_sequence=sequence,
                    pickup_type=boarding,
                    drop_off_type=boarding,
                )
            )
            surviving_stops.append(stop)

        if len(trip_stop_times) < 2:
            logger.warning(
                "trip %s/%s has fewer than 2 usable stop times after filtering, dropping the trip",
                line_id,
                connection_number,
            )
            continue

        calendar = timetable.calendars.get((jdf_ids.get(line_id, ""), connection_number))
        if calendar is None:
            # CIS does not know this trip. Its fixed codes are the weaker
            # source -- that is the whole reason CIS is consulted first -- but
            # for a trip with no calendar at all they beat dropping it.
            logger.warning(
                "line %s trip %s is not in CIS, falling back to its fixed codes %r",
                line_id,
                connection_number,
                sorted(connection.fixed_codes),
            )
            service = service_from_codes(connection.fixed_codes)
        else:
            service = service_from_dates(calendar.days, start, end, calendar.origin)

        if not service.runs_at_all:
            # Nothing places this trip in the year: either it carries no
            # calendar code at all, or CIS says it runs on no day between now
            # and the feed's horizon. Dropping one trip beats the alternative:
            # this used to raise through build_feed and take the whole feed
            # down, so a single unrecognised code meant publishing nothing.
            logger.warning(
                "trip %s/%s runs on no day of the feed's validity window, dropping the trip",
                line_id,
                connection_number,
            )
            continue

        # Counted here rather than where the service was chosen, so that the
        # summaries below add up to the trips the feed actually carries.
        if calendar is None:
            from_codes += 1
        else:
            from_cis += 1
        if not service.days and not service.holidays:
            no_weekly_pattern += 1

        sid = service.service_id
        trips.append(
            Trip(
                route_id=route_id(line_id),
                service_id=sid,
                trip_id=tid,
                trip_headsign=names.get(surviving_stops[-1].stop_id, ""),
                direction_id=timetable.directions.get(key, 0),
                wheelchair_accessible=1 if connection.low_floor else 0,
            )
        )
        # Two different services on one id would silently give one of them the
        # other's days: setdefault keeps the first and every later trip points
        # at a calendar row that is not its own. It cannot happen while a
        # service with exceptions carries the origin that tells it apart, which
        # is exactly the property worth failing on if it ever stops holding.
        seen = services.setdefault(sid, service)
        if seen != service:
            raise FeedBuildError(f"two different services share the id {sid!r}")
        stop_times.extend(trip_stop_times)

    total = from_cis + from_codes
    logger.log(
        # A handful of trips CIS has not heard of is the normal state of
        # affairs; a tenth of the network would mean the two sources have
        # drifted far enough apart that the days of operation in this feed can
        # no longer be trusted, and someone has to look.
        logging.ERROR if from_codes > total * MAX_FALLBACK_SHARE else logging.INFO,
        "calendars: %d trips from CIS, %d fell back to the API's fixed codes",
        from_cis,
        from_codes,
    )
    # Not an error and not a threshold: a trip whose CIS days are too few for
    # any weekday to carry a majority is described entirely by
    # calendar_dates.txt, which is what the weeks around a timetable change look
    # like. Its own prefix, so that an operator alerting on drift in the line
    # above is not woken by the seasons.
    if no_weekly_pattern:
        logger.warning(
            "seasonal calendars: %d trips run for too few days to have a weekly "
            "pattern, and stop when the timetable they belong to does",
            no_weekly_pattern,
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
    compares this set across rebuilds (see :mod:`dpmp_gtfs.static.service_watch`).
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


def apply_shapes(
    trips: list[Trip],
    stop_times: list[StopTime],
    shapes: dict[StopSequence, TripGeometry],
) -> tuple[list[Trip], list[StopTime]]:
    """Attach shape ids and distances to trips that have routed geometry."""
    by_trip: dict[str, list[StopTime]] = {}
    for st in stop_times:
        by_trip.setdefault(st.trip_id, []).append(st)

    sequence_of = {
        trip_id: tuple(st.stop_id for st in sorted(times, key=lambda s: s.stop_sequence))
        for trip_id, times in by_trip.items()
    }

    updated_trips = [
        replace(trip, shape_id=shape.shape_id)
        if (shape := shapes.get(sequence_of.get(trip.trip_id, ())))
        else trip
        for trip in trips
    ]

    updated_times: list[StopTime] = []
    for st in stop_times:
        shape = shapes.get(sequence_of.get(st.trip_id, ()))
        if shape is None or st.stop_sequence >= len(shape.stop_distances):
            updated_times.append(st)
            continue
        distance = shape.stop_distances[st.stop_sequence]
        updated_times.append(replace(st, shape_dist_traveled=f"{distance:.1f}"))

    return updated_trips, updated_times


def iter_missing_stop_references(feed: Feed) -> Iterator[str]:
    """Stop ids used by stop_times that no stop declares.

    Cheap internal consistency check, run before writing so a broken feed is
    never published.
    """
    known = {s.stop_id for s in feed.stops}
    for st in feed.stop_times:
        if st.stop_id not in known:
            yield st.stop_id


def stop_sequences(
    stop_times: list[StopTime], stops: list[Stop]
) -> dict[StopSequence, list[LatLon]]:
    """Distinct stop sequences and their coordinates.

    Trips overwhelmingly share routes: 2,728 trips collapse to roughly 220
    sequences, which is what makes routing geometry affordable.
    """
    position = {s.stop_id: (s.stop_lat, s.stop_lon) for s in stops}

    by_trip: dict[str, list[tuple[int, str]]] = {}
    for st in stop_times:
        by_trip.setdefault(st.trip_id, []).append((st.stop_sequence, st.stop_id))

    sequences: dict[StopSequence, list[LatLon]] = {}
    for entries in by_trip.values():
        sequence = tuple(stop_id for _, stop_id in sorted(entries))
        if len(sequence) < 2 or any(s not in position for s in sequence):
            continue
        sequences.setdefault(sequence, [position[s] for s in sequence])

    return sequences


def used_platforms(timetable: Timetable) -> dict[int, set[int]]:
    """``{stop: {platform, ...}}`` as actually referenced by timetables.

    The timetable is the authority on which platforms exist; ``/stops`` does
    not describe them at all.
    """
    used: dict[int, set[int]] = {}
    for connection in timetable.connections.values():
        for stop in connection.stops:
            if stop.platform_id.isdigit():
                used.setdefault(stop.stop_id, set()).add(int(stop.platform_id))
    return used


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
        t = stop.departure
        raw = t.hour * 3600 + t.minute * 60 + t.second
        if previous is not None and raw < previous:
            offset += DAY
        previous = raw
        out.append(raw + offset)

    return out
