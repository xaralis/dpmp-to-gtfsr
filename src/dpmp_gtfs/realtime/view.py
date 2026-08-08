"""A readable view of what the vehicles are doing.

GTFS-Realtime is built for machines that already hold the static feed: it
carries stop *ids* and leaves resolving them to the consumer. Anything that
wants to show a vehicle to a person -- a map, a departure board, a bot -- has
to join those ids back against the timetable to say "left Masarykovo nám.,
next Náměstí Republiky, 46s late".

Doing that join here rather than in each client keeps a single answer to
questions like "which stop is next", and means the browser does not need a
copy of the timetable to draw a popup.

This is deliberately *not* GTFS-RT. Consumers wanting the standard should take
``/gtfs-rt.pb``; this is the same data shaped for reading.
"""

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any

from dpmp_gtfs.api.models import Bus
from dpmp_gtfs.timeutil import PRAGUE, format_clock
from dpmp_gtfs.upstream import TROLLEYBUS_LINES

from .index import ScheduledStop, StaticIndex
from .tracker import DelayTracker


def build_vehicle_views(
    buses: list[Bus],
    index: StaticIndex,
    tracker: DelayTracker,
    now: dt.datetime | None = None,
) -> list[VehicleView]:
    """Join live vehicles against the timetable into something displayable."""
    now = now or dt.datetime.now(dt.UTC)
    views: list[VehicleView] = []

    for bus in buses:
        trip = index.lookup(bus.line, bus.connection_no)
        if trip is None:
            continue

        # ``current_stop_number`` is the stop being approached, so its index is
        # also the boundary: everything before it has been served.
        position = trip.locate(bus.current_stop, bus.current_station)
        previous = trip.stops[position - 1] if position is not None and position > 0 else None
        upcoming = (
            trip.stops[position] if position is not None and position < len(trip.stops) else None
        )

        delay = tracker.delay_for(bus, now)

        views.append(
            VehicleView(
                vehicle_id=bus.vid,
                line=bus.line_name,
                trolleybus=bus.line in TROLLEYBUS_LINES,
                trip_id=trip.trip_id,
                route_id=trip.route_id,
                destination=bus.destination_name,
                latitude=bus.gps_latitude,
                longitude=bus.gps_longitude,
                bearing=bus.gps_course,
                reported_at=bus.state_dtime.astimezone(PRAGUE).isoformat(timespec="seconds"),
                delay_seconds=delay.seconds if delay else None,
                delay_measured=bool(delay and delay.measured),
                previous_stop=_stop_view(previous) if previous else None,
                next_stop=_stop_view(upcoming) if upcoming else None,
                stops_total=len(trip.stops),
                stop_index=position,
            )
        )

    return sorted(views, key=lambda v: (len(v.line), v.line, v.vehicle_id))


def as_payload(views: list[VehicleView], built_at: dt.datetime | None) -> dict[str, Any]:
    return {
        "built_at": built_at.isoformat(timespec="seconds") if built_at else None,
        "count": len(views),
        "vehicles": [asdict(v) for v in views],
    }


@dataclass(frozen=True, slots=True)
class VehicleView:
    vehicle_id: str
    line: str
    trolleybus: bool
    trip_id: str
    route_id: str
    destination: str
    latitude: float
    longitude: float
    bearing: float | None
    reported_at: str
    delay_seconds: int | None
    """Positive when late. ``None`` when nothing supports a claim either way --
    typically a vehicle waiting at its first stop, which is a large share of
    them at any moment."""
    delay_measured: bool
    """True when observed from a stop-to-stop transition, false when it is the
    conservative lower bound derived from the countdown."""
    previous_stop: StopView | None
    next_stop: StopView | None
    stops_total: int
    stop_index: int | None


@dataclass(frozen=True, slots=True)
class StopView:
    id: str
    name: str
    scheduled: str
    """Timetabled time at this stop, ``HH:MM``. May read past 24:00 on a trip
    that crosses midnight, as GTFS does."""


def _stop_view(stop: ScheduledStop) -> StopView:
    return StopView(id=stop.stop_id, name=stop.name, scheduled=format_clock(stop.seconds))
