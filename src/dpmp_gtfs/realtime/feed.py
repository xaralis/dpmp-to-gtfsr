"""Builds the GTFS-Realtime FeedMessage from a /{provider}/vehicles snapshot."""

import datetime as dt
import logging

from google.transit import gtfs_realtime_pb2 as gtfsr

from dpmp_gtfs.api.models import Vehicle
from dpmp_gtfs.ids import station_id, stop_id
from dpmp_gtfs.timeutil import format_gtfs_time, service_day_date

from .index import ScheduledTrip, StaticIndex

logger = logging.getLogger(__name__)

DELAY_DECAY_PER_STOP = 0.9
"""How much of a delay is assumed to survive each further stop.

A vehicle that is late tends to catch up a little at every stop, so projecting
the measured delay unchanged to the end of the trip overstates it. This is a
smoothing assumption, not a measurement.
"""


def project_delay(delay: dt.timedelta, stops_ahead: int) -> int:
    """The delay expected ``stops_ahead`` stops from now, in whole seconds."""
    return int(delay.total_seconds() * DELAY_DECAY_PER_STOP**stops_ahead)


def build_feed_message(
    vehicles: list[Vehicle],
    index: StaticIndex,
    now: dt.datetime | None = None,
) -> gtfsr.FeedMessage:
    """Assemble vehicle positions and trip updates for one snapshot."""
    now = now or dt.datetime.now(dt.UTC)

    message = gtfsr.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.incrementality = gtfsr.FeedHeader.FULL_DATASET
    message.header.timestamp = int(now.timestamp())

    unmatched = 0
    reported_at = int(now.timestamp())

    for vehicle in vehicles:
        trip = index.lookup(vehicle.line_id, vehicle.connection_id)
        if trip is None:
            unmatched += 1
            logger.debug(
                "vehicle %s on line %s trip %s is not in the static feed",
                vehicle.vid,
                vehicle.line_id,
                vehicle.connection_id,
            )
            continue

        first_departure = trip.stops[0].seconds if trip.stops else 0
        service_date = _service_date(now, first_departure)

        vehicle_descriptor = gtfsr.VehicleDescriptor(id=vehicle.vid, label=vehicle.vid)

        # Where the vehicle is along this trip, resolved once and used by both
        # the position and the predictions so they cannot disagree.
        current = _current_stop_id(vehicle)
        position_now = trip.locate(current or None, vehicle.next_stop_id)

        # --- vehicle position ---
        position = gtfsr.Position(latitude=vehicle.gps_latitude, longitude=vehicle.gps_longitude)

        vehicle_position = gtfsr.VehiclePosition(
            trip=_descriptor(trip, service_date, first_departure),
            vehicle=vehicle_descriptor,
            position=position,
            timestamp=reported_at,
            stop_id=current,
            current_status=(
                gtfsr.VehiclePosition.STOPPED_AT
                if vehicle.on_station
                else gtfsr.VehiclePosition.IN_TRANSIT_TO
            ),
        )
        if position_now is not None:
            vehicle_position.current_stop_sequence = trip.stops[position_now].sequence

        entity = message.entity.add(id=f"v{vehicle.vid}")
        entity.vehicle.CopyFrom(vehicle_position)

        # --- trip update ---
        delay = vehicle.delay
        if delay is None:
            # No evidence either way. Emitting zero here would assert
            # punctuality for every vehicle the upstream declined to describe.
            continue

        update = gtfsr.TripUpdate(
            trip=_descriptor(trip, service_date, first_departure),
            vehicle=vehicle_descriptor,
            timestamp=reported_at,
            delay=int(delay.total_seconds()),
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
                stu.schedule_relationship = gtfsr.TripUpdate.StopTimeUpdate.SCHEDULED

        entity = message.entity.add(id=f"t{vehicle.vid}")
        entity.trip_update.CopyFrom(update)

    if unmatched:
        logger.info("%d of %d vehicles had no matching static trip", unmatched, len(vehicles))

    return message


def _current_stop_id(vehicle: Vehicle) -> str:
    """The stop id to publish for where a vehicle is heading.

    The platform when the upstream names one; otherwise the parent station,
    which is still a real, resolvable id in the static feed -- unlike a
    fabricated platform or an empty string, either of which a consumer could
    not join back to anything.
    """
    if vehicle.next_stop_id is None:
        return ""
    if vehicle.next_stop_platform_id is not None:
        return stop_id(vehicle.next_stop_id, vehicle.next_stop_platform_id)
    return station_id(vehicle.next_stop_id)


def _descriptor(
    trip: ScheduledTrip, service_date: str, first_departure: int
) -> gtfsr.TripDescriptor:
    """Identify a trip run.

    ``start_date`` and ``start_time`` together disambiguate which run of a trip
    this is, which matters around midnight when two service days overlap.
    """
    return gtfsr.TripDescriptor(
        trip_id=trip.trip_id,
        route_id=trip.route_id,
        start_date=service_date,
        start_time=format_gtfs_time(first_departure),
        schedule_relationship=gtfsr.TripDescriptor.SCHEDULED,
    )


def _service_date(now: dt.datetime, first_departure: int) -> str:
    """The service date a trip belongs to, as ``YYYYMMDD``.

    Answered by comparing the snapshot's own clock against the trip's
    schedule rather than by inspecting the schedule alone. A trip that departs
    at 23:58 and arrives at 00:52 has a first departure below 24:00, yet a
    vehicle running it at 00:30 still belongs to the previous service day --
    and the consumer matching on ``(trip_id, start_date)`` has to be told so.
    """
    return service_day_date(now, first_departure).strftime("%Y%m%d")
