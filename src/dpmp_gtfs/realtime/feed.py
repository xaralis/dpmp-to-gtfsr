"""Builds the GTFS-Realtime FeedMessage from a /api/buses snapshot."""

import datetime as dt
import logging

from google.transit import gtfs_realtime_pb2 as rt

from dpmp_gtfs.api.models import Bus
from dpmp_gtfs.timeutil import format_gtfs_time, service_day_date

from .index import ScheduledTrip, StaticIndex
from .tracker import DelayTracker, project_delay

logger = logging.getLogger(__name__)


def _service_date(bus_time: dt.datetime, first_departure: int) -> str:
    """The service date a trip belongs to, as ``YYYYMMDD``.

    Answered by comparing the vehicle's clock against the trip's own schedule
    rather than by inspecting the schedule alone. A trip that departs at 23:58
    and arrives at 00:52 has a first departure below 24:00, yet a vehicle
    running it at 00:30 still belongs to the previous service day -- and the
    consumer matching on ``(trip_id, start_date)`` has to be told so.
    """
    return service_day_date(bus_time, first_departure).strftime("%Y%m%d")


def _descriptor(trip: ScheduledTrip, service_date: str, first_departure: int) -> rt.TripDescriptor:
    """Identify a trip run.

    ``start_date`` and ``start_time`` together disambiguate which run of a trip
    this is, which matters around midnight when two service days overlap.
    """
    return rt.TripDescriptor(
        trip_id=trip.trip_id,
        route_id=trip.route_id,
        start_date=service_date,
        start_time=format_gtfs_time(first_departure),
        schedule_relationship=rt.TripDescriptor.SCHEDULED,
    )


def build_feed_message(
    buses: list[Bus],
    index: StaticIndex,
    tracker: DelayTracker,
    now: dt.datetime | None = None,
) -> rt.FeedMessage:
    """Assemble vehicle positions and trip updates for one snapshot."""
    now = now or dt.datetime.now(dt.UTC)

    message = rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.incrementality = rt.FeedHeader.FULL_DATASET
    message.header.timestamp = int(now.timestamp())

    unmatched = 0

    for bus in buses:
        trip = index.lookup(bus.line, bus.connection_no)
        if trip is None:
            unmatched += 1
            logger.debug(
                "vehicle %s on line %s trip %s is not in the static feed",
                bus.vid,
                bus.line_name,
                bus.connection_no,
            )
            continue

        first_departure = trip.stops[0].seconds if trip.stops else 0
        service_date = _service_date(bus.state_dtime, first_departure)
        reported_at = int(bus.state_dtime.timestamp())

        vehicle_descriptor = rt.VehicleDescriptor(id=bus.vid, label=bus.vid)

        # Where the vehicle is along this trip, resolved once and used by both
        # the position and the predictions so they cannot disagree.
        current = bus.current_stop
        position_now = trip.locate(current, bus.current_station)

        # --- vehicle position ---
        position = rt.Position(latitude=bus.gps_latitude, longitude=bus.gps_longitude)
        if bus.gps_course is not None:
            position.bearing = bus.gps_course

        vehicle = rt.VehiclePosition(
            trip=_descriptor(trip, service_date, first_departure),
            vehicle=vehicle_descriptor,
            position=position,
            timestamp=reported_at,
            stop_id=current or "",
            current_status=rt.VehiclePosition.IN_TRANSIT_TO,
        )
        if position_now is not None:
            vehicle.current_stop_sequence = trip.stops[position_now].sequence

        entity = message.entity.add(id=f"v{bus.vid}")
        entity.vehicle.CopyFrom(vehicle)

        # --- trip update ---
        delay = tracker.delay_for(bus, now)
        if delay is None:
            # No evidence either way. Emitting a delay of zero here would
            # assert punctuality for roughly 42% of the fleet.
            continue

        update = rt.TripUpdate(
            trip=_descriptor(trip, service_date, first_departure),
            vehicle=vehicle_descriptor,
            timestamp=reported_at,
            delay=delay.seconds,
        )

        # Predictions for the rest of the trip, starting from where the vehicle
        # is now.
        if position_now is not None:
            for offset, scheduled in enumerate(trip.stops[position_now:]):
                predicted = project_delay(delay, offset)
                stu = update.stop_time_update.add()
                stu.stop_sequence = scheduled.sequence
                stu.stop_id = scheduled.stop_id
                stu.arrival.delay = predicted
                stu.departure.delay = predicted
                stu.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SCHEDULED

        entity = message.entity.add(id=f"t{bus.vid}")
        entity.trip_update.CopyFrom(update)

    if unmatched:
        logger.info("%d of %d vehicles had no matching static trip", unmatched, len(buses))

    return message
