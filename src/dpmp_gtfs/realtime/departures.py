"""What leaves a stop next, timetable joined with live delays.

The reverse of :mod:`dpmp_gtfs.realtime.view`. That one starts from a vehicle
and asks where it is; this starts from a stop and asks what is coming. Both
joins happen here rather than in the browser, so a departure board is one
request and needs no copy of the timetable.

A scheduled departure only carries a delay when a vehicle is actually out
running that trip. Trips whose vehicle has not been dispatched yet -- most of
the board, most of the time -- are shown as timetabled, with nothing claimed
about them either way.
"""

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any

from dpmp_gtfs.timeutil import PRAGUE, format_clock

from .index import StaticIndex
from .view import VehicleView


@dataclass(frozen=True, slots=True)
class Departure:
    line: str
    trip_id: str
    headsign: str
    """The trip's final stop -- what a passenger reads as its direction."""
    scheduled: str
    """Timetabled departure, ``HH:MM``."""
    expected: str
    """Departure once the measured delay is applied. Equal to ``scheduled``
    when nothing is known about the vehicle."""
    in_seconds: int
    """Seconds until the expected departure, for a countdown."""
    delay_seconds: int | None
    """``None`` when no vehicle is running this trip yet, which is not the same
    as "on time"."""
    delay_measured: bool
    """False when the delay is the countdown-derived lower bound."""
    at_platform: str


def build_departures(
    stop_id: str,
    index: StaticIndex,
    vehicles: list[VehicleView],
    now: dt.datetime | None = None,
    limit: int = 8,
) -> list[Departure]:
    """The next departures from one stop, soonest first."""
    now = now or dt.datetime.now(dt.UTC)

    # A trip has at most one vehicle on it, so this is the whole realtime join.
    live = {v.trip_id: v for v in vehicles if v.delay_seconds is not None}

    board: list[Departure] = []
    for when, trip, seconds in index.departures(stop_id, now, limit):
        vehicle = live.get(trip.trip_id)
        delay = vehicle.delay_seconds if vehicle else None
        expected = when + dt.timedelta(seconds=delay or 0)

        board.append(
            Departure(
                line=trip.line,
                trip_id=trip.trip_id,
                headsign=trip.headsign,
                scheduled=format_clock(seconds),
                expected=expected.astimezone(PRAGUE).strftime("%H:%M"),
                in_seconds=max(0, int((expected - now).total_seconds())),
                delay_seconds=delay,
                delay_measured=bool(vehicle and vehicle.delay_measured),
                at_platform=stop_id,
            )
        )

    return board


def as_payload(
    stop_id: str, name: str, board: list[Departure], built_at: dt.datetime | None
) -> dict[str, Any]:
    return {
        "stop_id": stop_id,
        "name": name,
        "built_at": built_at.isoformat(timespec="seconds") if built_at else None,
        "count": len(board),
        "departures": [asdict(d) for d in board],
    }
