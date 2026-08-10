"""Turns a crawled :class:`Timetable` into GTFS records."""

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from dpmp_gtfs.api.models import Connection, ConnectionStop
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

from .calendar import (
    calendar_exceptions,
    numbered_services,
    service_from_codes,
    service_from_dates,
)
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
            logger.warning(
                "stop %s (%s) has no coordinates, skipping", api_stop.id, api_stop.name
            )
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
        logger.error(
            "stop %s is used by timetables but is unknown to /stops; its trips "
            "will reference a stop that does not exist",
            number,
        )

    return stops


def stops_without_coordinates(timetable: Timetable) -> set[int]:
    """Ids of stops ``/stops`` describes but without a position.

    Shared between :func:`build_stops`, which drops them from ``stops.txt``,
    and :func:`build_trips_and_stop_times`, which must drop the matching
    ``stop_times.txt`` rows -- otherwise the two files disagree about which
    stops exist.
    """
    return {s.id for s in timetable.stops if s.gps_latitude is None or s.gps_longitude is None}


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
    trip_services: list[Service] = []
    """One per entry in ``trips``. The service ids cannot be handed out until
    every service is known, because what distinguishes two services sharing a
    weekly pattern is the other ones -- see :func:`numbered_services`."""
    stop_times: list[StopTime] = []
    names = {s.id: s.name for s in timetable.stops}
    on_request_stops = {s.id for s in timetable.stops if s.on_request}
    missing_coords = stops_without_coordinates(timetable)
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
            if stop.stop_id in missing_coords:
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
                "trip %s/%s has fewer than 2 usable stop times after filtering, "
                "dropping the trip",
                line_id,
                connection_number,
            )
            continue

        dates = timetable.calendars.get((jdf_ids.get(line_id, ""), connection_number))
        if dates is None:
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
            service = service_from_dates(dates, start, end)

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
        if dates is None:
            from_codes += 1
        else:
            from_cis += 1
        if not service.days and not service.holidays:
            no_weekly_pattern += 1

        trips.append(
            Trip(
                route_id=route_id(line_id),
                service_id="",
                trip_id=tid,
                trip_headsign=names.get(surviving_stops[-1].stop_id, ""),
                direction_id=timetable.directions.get(key, 0),
                wheelchair_accessible=1 if connection.low_floor else 0,
            )
        )
        trip_services.append(service)
        stop_times.extend(trip_stop_times)

    numbering = numbered_services(set(trip_services))
    named = [
        replace(trip, service_id=numbering[service].service_id)
        for trip, service in zip(trips, trip_services, strict=True)
    ]

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
    logger.log(
        # A trip whose CIS days are too few for any weekday to carry a majority
        # is described entirely by calendar_dates.txt. That is normal for the
        # weeks either side of a timetable change; at a tenth of the network it
        # means the timetable this feed describes is running out.
        logging.ERROR if no_weekly_pattern > len(named) * MAX_FALLBACK_SHARE else logging.INFO,
        "calendars: %d trips run on too few days to have a weekly pattern",
        no_weekly_pattern,
    )
    return named, stop_times, sorted(numbering.values(), key=lambda s: s.service_id)


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
