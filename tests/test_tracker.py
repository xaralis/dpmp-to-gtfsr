"""Tests for delay measurement.

The central claim under test is that ``time_difference`` never reaches the
feed as a delay. It is a countdown, and publishing it would report the wrong
quantity to every consumer.
"""

import datetime as dt
import glob
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from dpmp_gtfs.api.models import Bus
from dpmp_gtfs.realtime.index import ScheduledStop, ScheduledTrip, StaticIndex
from dpmp_gtfs.realtime.tracker import (
    MEASUREMENT_TTL,
    DelayTracker,
    service_day_seconds,
)

SNAPSHOTS = Path(__file__).parent / "fixtures" / "snapshots"


def _index() -> StaticIndex:
    """A one-trip index: line 9 connection 115, calling at stations 1..3."""
    trip = ScheduledTrip(
        trip_id="L9C115",
        route_id="L9",
        stops=(
            # 19:00, 19:05, 19:10
            ScheduledStop("S1P1", 1, 0, 19 * 3600),
            ScheduledStop("S2P1", 2, 1, 19 * 3600 + 300),
            ScheduledStop("S3P1", 3, 2, 19 * 3600 + 600),
        ),
    )
    return StaticIndex({"L9C115": trip})


def _bus(**overrides: Any) -> Bus:
    base = {
        "vid": "105",
        "state_dtime": "2026-08-07 17:00:00",  # 19:00 Prague
        "line_name": "9",
        "line_direction": "S12",
        "destination_name": "x",
        "last_stop_number": "0",
        "last_stop_name": None,
        "current_stop_number": "101",
        "current_stop_name": "x",
        "current_stop_scheduled_departure": "19:00:00",
        "time_difference": None,
        "connection_no": 115,
        "gps_latitude": 50.0,
        "gps_longitude": 15.0,
    }
    return Bus.model_validate(base | overrides)


# --- service day arithmetic -------------------------------------------------


def test_service_day_seconds_for_an_ordinary_time() -> None:
    moment = dt.datetime(2026, 8, 7, 17, 0, tzinfo=dt.UTC)  # 19:00 Prague
    assert service_day_seconds(moment, 19 * 3600) == 19 * 3600


def test_service_day_seconds_resolves_a_past_midnight_trip() -> None:
    """A vehicle seen at 00:10 on a trip scheduled for 24:10 belongs to the
    previous service day, not ten minutes into a new one."""
    moment = dt.datetime(2026, 8, 7, 22, 10, tzinfo=dt.UTC)  # 00:10 Prague, next day
    scheduled = 24 * 3600 + 600  # 24:10
    assert service_day_seconds(moment, scheduled) == scheduled


# --- measurement ------------------------------------------------------------


def test_no_delay_before_any_transition_is_observed() -> None:
    tracker = DelayTracker(_index())
    bus = _bus()
    tracker.observe([bus])
    assert tracker.delay_for(bus, bus.state_dtime) is None


def test_transition_produces_a_measured_delay() -> None:
    tracker = DelayTracker(_index())
    tracker.observe([_bus(last_stop_number="0")])

    # Left station 1 at 19:02 Prague; it was due out at 19:00 -> 120s late.
    late = _bus(state_dtime="2026-08-07 17:02:00", last_stop_number="1")
    assert tracker.observe([late]) == 1

    delay = tracker.delay_for(late, late.state_dtime)
    assert delay is not None
    assert delay.seconds == 120
    assert delay.measured is True
    assert delay.at_stop == "S1P1"


def test_a_vehicle_running_early_measures_a_negative_delay() -> None:
    tracker = DelayTracker(_index())
    tracker.observe([_bus(last_stop_number="0")])

    early = _bus(state_dtime="2026-08-07 16:59:10", last_stop_number="1")
    tracker.observe([early])

    delay = tracker.delay_for(early, early.state_dtime)
    assert delay is not None
    assert delay.seconds == -50


def test_countdown_is_never_reported_as_a_measured_delay() -> None:
    """The regression this whole module exists for.

    A vehicle 84s from its scheduled departure must not be described as 84
    seconds early -- that number is a countdown, not a delay.
    """
    tracker = DelayTracker(_index())
    bus = _bus(time_difference="00:01:24")  # 84s until departure
    tracker.observe([bus])

    delay = tracker.delay_for(bus, bus.state_dtime)
    assert delay is not None
    assert delay.measured is False
    assert delay.seconds == 0, "a positive countdown proves nothing about lateness"


def test_an_elapsed_countdown_is_a_lower_bound_on_lateness() -> None:
    tracker = DelayTracker(_index())
    bus = _bus(time_difference="-00:01:24")  # 84s past due, still not gone
    tracker.observe([bus])

    delay = tracker.delay_for(bus, bus.state_dtime)
    assert delay is not None
    assert delay.seconds == 84
    assert delay.measured is False


def test_a_measurement_beats_the_countdown_fallback() -> None:
    tracker = DelayTracker(_index())
    tracker.observe([_bus(last_stop_number="0")])
    moved = _bus(
        state_dtime="2026-08-07 17:02:00",
        last_stop_number="1",
        time_difference="-00:10:00",  # would suggest 600s
    )
    tracker.observe([moved])

    delay = tracker.delay_for(moved, moved.state_dtime)
    assert delay is not None
    assert delay.seconds == 120, "measured value must win"
    assert delay.measured is True


def test_a_stale_measurement_is_discarded() -> None:
    tracker = DelayTracker(_index())
    tracker.observe([_bus(last_stop_number="0")])
    moved = _bus(state_dtime="2026-08-07 17:02:00", last_stop_number="1")
    tracker.observe([moved])

    later = moved.state_dtime + MEASUREMENT_TTL + dt.timedelta(seconds=1)
    assert tracker.delay_for(moved, later) is None


def test_state_is_dropped_when_a_vehicle_disappears() -> None:
    tracker = DelayTracker(_index())
    tracker.observe([_bus()])
    assert tracker._vehicles
    tracker.observe([])
    assert not tracker._vehicles


def test_starting_a_new_trip_resets_history() -> None:
    """Delay from a finished trip must not leak into the next one."""
    index = StaticIndex(
        {
            "L9C115": _index().lookup(9, 115),  # type: ignore[dict-item]
            "L9C117": ScheduledTrip("L9C117", "L9", (ScheduledStop("S1P1", 1, 0, 20 * 3600),)),
        }
    )
    tracker = DelayTracker(index)
    tracker.observe([_bus(last_stop_number="0")])
    tracker.observe([_bus(state_dtime="2026-08-07 17:02:00", last_stop_number="1")])

    next_trip = _bus(connection_no=117, last_stop_number="1")
    tracker.observe([next_trip])
    assert tracker.delay_for(next_trip, next_trip.state_dtime) is None


def test_a_vehicle_on_an_unknown_trip_is_ignored() -> None:
    tracker = DelayTracker(_index())
    tracker.observe([_bus(connection_no=9999)])
    assert not tracker._vehicles


# --- recorded snapshots -----------------------------------------------------


def _snapshots() -> list[tuple[dt.datetime, list[Bus]]]:
    out = []
    for path in sorted(glob.glob(str(SNAPSHOTS / "*.json"))):
        payload = json.loads(Path(path).read_text(encoding="utf8"))
        out.append(
            (
                dt.datetime.fromisoformat(payload["recorded_at"]),
                [Bus.model_validate(b) for b in payload["buses"]],
            )
        )
    return out


@pytest.mark.skipif(not SNAPSHOTS.exists(), reason="no recorded snapshots")
def test_recorded_snapshots_show_vehicles_moving_between_stops() -> None:
    """Sanity check on the fixture itself: without transitions the tracker
    could pass its unit tests and still measure nothing in production."""
    snapshots = _snapshots()
    assert len(snapshots) >= 10

    transitions = 0
    for (_, before), (_, after) in pairwise(snapshots):
        old = {b.vid: b.last_station for b in before}
        for bus in after:
            if bus.last_station is not None and old.get(bus.vid, ...) != bus.last_station:
                transitions += 1

    assert transitions > 50, f"only {transitions} transitions in the recording"
