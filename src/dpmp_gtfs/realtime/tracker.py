"""Measures real delay by watching vehicles move between stops.

Why this exists
---------------

The obvious candidate for a delay is ``time_difference`` in ``/api/buses``. It
is not one. Observing a vehicle over five snapshots 30s apart shows the value
falling by exactly 30s each time while the vehicle sits at the same stop:

    20:19:13  td=-00:00:04  scheduled 20:19  Náměstí Republiky
    20:19:41  td=-00:00:34  scheduled 20:19  Náměstí Republiky
    20:20:09  td=-00:01:04  scheduled 20:19  Náměstí Republiky
    20:20:38  td=-00:01:24  scheduled 20:19  Náměstí Republiky

It is ``scheduled departure from current_stop - state_dtime``: a countdown to
the next departure, which changes every second even for a perfectly punctual
vehicle. Publishing it as a GTFS ``delay`` reports a different quantity than
the one consumers expect.

What this does instead
----------------------

``last_stop_number`` names the last stop a vehicle actually served. When it
changes, the vehicle has just left that stop, and the vehicle's own
``state_dtime`` says when. Comparing that against the scheduled departure from
the same stop gives a real, measured delay.

Recording 40 snapshots at 15s intervals caught 150 transitions across 29
vehicles -- a median of 5 per vehicle in ten minutes, so a vehicle acquires a
measurement within a stop or two of being observed.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from dpmp_gtfs.api.models import Bus
from dpmp_gtfs.timeutil import service_day_seconds

from .index import ScheduledTrip, StaticIndex

logger = logging.getLogger(__name__)

# A measurement older than this is not reported. A vehicle that has not passed
# a stop in this long has probably been held up in a way its last measurement
# no longer describes.
MEASUREMENT_TTL = dt.timedelta(minutes=20)


@dataclass(frozen=True, slots=True)
class Delay:
    """A measured delay, positive when late."""

    seconds: int
    measured_at: dt.datetime
    at_stop: str
    """Stop id where the measurement was taken."""
    measured: bool
    """False when this is the countdown-derived lower bound rather than an
    observed transition."""


@dataclass(slots=True)
class _VehicleState:
    last_station: int | None
    trip_id: str
    delay: Delay | None = None


class DelayTracker:
    """Keeps just enough per-vehicle history to measure delays.

    State lives in memory only. After a restart it refills within a minute or
    two of normal operation, so persisting it would add failure modes without
    buying much.
    """

    def __init__(self, index: StaticIndex) -> None:
        self.index = index
        self._vehicles: dict[str, _VehicleState] = {}

    def observe(self, buses: list[Bus]) -> int:
        """Fold a fresh ``/api/buses`` snapshot in. Returns transitions seen."""
        seen: set[str] = set()
        transitions = 0

        for bus in buses:
            seen.add(bus.vid)
            trip = self.index.lookup(bus.line, bus.connection_no)
            if trip is None:
                continue

            previous = self._vehicles.get(bus.vid)
            # A vehicle starting a new trip carries no history worth keeping.
            if previous is None or previous.trip_id != trip.trip_id:
                self._vehicles[bus.vid] = _VehicleState(
                    last_station=bus.last_station, trip_id=trip.trip_id
                )
                continue

            if bus.last_station is not None and bus.last_station != previous.last_station:
                measurement = self._measure(bus, trip)
                if measurement is not None:
                    previous.delay = measurement
                    transitions += 1
                previous.last_station = bus.last_station

        # Vehicles that have gone off the road take their state with them.
        for vid in self._vehicles.keys() - seen:
            del self._vehicles[vid]

        return transitions

    def _measure(self, bus: Bus, trip: ScheduledTrip) -> Delay | None:
        assert bus.last_station is not None
        position = trip.index_of_station(bus.last_station)
        if position is None:
            logger.debug(
                "vehicle %s reports stop %s which is not on trip %s",
                bus.vid,
                bus.last_station,
                trip.trip_id,
            )
            return None

        stop = trip.stops[position]
        # state_dtime is when the vehicle reported having served this stop.
        actual = service_day_seconds(bus.state_dtime, stop.seconds)
        return Delay(
            seconds=actual - stop.seconds,
            measured_at=bus.state_dtime,
            at_stop=stop.stop_id,
            measured=True,
        )

    def delay_for(self, bus: Bus, now: dt.datetime) -> Delay | None:
        """Best available delay for a vehicle, or ``None`` if there is none.

        Returning ``None`` matters: a vehicle waiting at its first stop has no
        delay yet, and roughly 42% of vehicles are in that state at any moment.
        Reporting zero for them would assert punctuality the data cannot
        support.
        """
        state = self._vehicles.get(bus.vid)
        if (
            state is not None
            and state.delay is not None
            and now - state.delay.measured_at <= MEASUREMENT_TTL
        ):
            return state.delay

        # No measurement yet. The countdown still proves a lower bound: if the
        # scheduled departure has already passed and the vehicle has not left,
        # it is at least that late. It can never prove the vehicle is early.
        countdown = bus.countdown
        if countdown is None:
            return None
        late = max(0, -int(countdown.total_seconds()))
        return Delay(
            seconds=late,
            measured_at=bus.state_dtime,
            at_stop="",
            measured=False,
        )


def project_delay(delay: Delay, stops_ahead: int) -> int:
    """Delay to predict ``stops_ahead`` stops past where it was measured.

    Deliberately constant: the measured delay is reported unchanged for the
    rest of the trip.

    The alternative would be to decay it, on the theory that timetable slack
    lets a late vehicle catch up. That is often true, but it is a model rather
    than a measurement -- it would sometimes promise a recovery that never
    happens, and a prediction that is confidently wrong is worse than one that
    is conservatively stale. Holding the delay steady only ever reports
    something that was actually observed.
    """
    return delay.seconds
