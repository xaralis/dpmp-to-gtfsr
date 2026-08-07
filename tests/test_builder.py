from __future__ import annotations

import pytest

from dpmp_gtfs.api.models import ConnectionStop
from dpmp_gtfs.ids import stop_id, trip_id
from dpmp_gtfs.static.builder import (
    ROUTE_TYPE_BUS,
    ROUTE_TYPE_TROLLEYBUS,
    TROLLEYBUS_LINES,
    Stop,
    StopTime,
    format_gtfs_time,
    prune_unserved_stops,
    stop_seconds,
)


def _stop(departure: str, *, number: int = 1, platform: int = 1, index: int = 0) -> ConnectionStop:
    return ConnectionStop.model_validate(
        {
            "number": number,
            "name": f"Stop {number}",
            "codes": [],
            "index": index,
            "distance": 0,
            "arrivalTime": "",
            "departureTime": departure,
            "platform": platform,
        }
    )


# --- time formatting --------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "formatted"),
    [
        (0, "00:00:00"),
        (4 * 3600 + 25 * 60, "04:25:00"),
        (23 * 3600 + 55 * 60, "23:55:00"),
        (24 * 3600 + 23 * 60, "24:23:00"),
        (25 * 3600, "25:00:00"),
    ],
)
def test_format_gtfs_time(seconds: int, formatted: str) -> None:
    assert format_gtfs_time(seconds) == formatted


# --- midnight rollover ------------------------------------------------------


def test_times_within_one_day_are_unchanged() -> None:
    stops = [_stop("0425"), _stop("0430"), _stop("0442")]
    assert stop_seconds(stops) == [15900, 16200, 16920]


def test_a_trip_crossing_midnight_keeps_counting_past_24h() -> None:
    """Line 3 trips 322/324 run 23:55 -> 00:23.

    Wrapping to 00:23 would describe a trip that arrives 23 hours before it
    departs, which consumers reject or render as an all-day journey.
    """
    stops = [_stop("2355"), _stop("2358"), _stop("0002"), _stop("0023")]
    seconds = stop_seconds(stops)

    assert [format_gtfs_time(s) for s in seconds] == [
        "23:55:00",
        "23:58:00",
        "24:02:00",
        "24:23:00",
    ]
    assert seconds == sorted(seconds), "times must increase along the trip"


def test_the_longest_real_midnight_trip() -> None:
    """Line 98 trip 9: 23:58 -> 00:52, the widest rollover in the network."""
    seconds = stop_seconds([_stop("2358"), _stop("0052")])
    assert [format_gtfs_time(s) for s in seconds] == ["23:58:00", "24:52:00"]


def test_night_trips_starting_after_midnight_are_left_alone() -> None:
    """Lines 98/99 have trips departing at 00:05 etc.

    Every one of them runs daily (codes 2+4+5), so which service day they are
    assigned to cannot change the feed -- and treating them as ordinary
    early-morning times is the simpler reading.
    """
    seconds = stop_seconds([_stop("0005"), _stop("0030"), _stop("0050")])
    assert [format_gtfs_time(s) for s in seconds] == ["00:05:00", "00:30:00", "00:50:00"]


# --- route classification ---------------------------------------------------


def test_trolleybus_lines_are_classified_separately() -> None:
    assert 6 not in TROLLEYBUS_LINES
    assert 1 in TROLLEYBUS_LINES
    assert ROUTE_TYPE_TROLLEYBUS == 11
    assert ROUTE_TYPE_BUS == 3


# --- pruning ----------------------------------------------------------------


def _platform(sid: str, parent: str) -> Stop:
    return Stop(sid, "x", 50.0, 15.0, 0, parent, "1", 0)


def _station(sid: str) -> Stop:
    return Stop(sid, "x", 50.0, 15.0, 1, "", "", 0)


def test_pruning_drops_platforms_with_no_service() -> None:
    stops = [_station("S7"), _platform("S7P1", "S7"), _platform("S7P2", "S7")]
    times = [StopTime("t", "08:00:00", "08:00:00", "S7P1", 0, 0, 0)]

    kept = {s.stop_id for s in prune_unserved_stops(stops, times)}
    assert kept == {"S7", "S7P1"}


def test_pruning_drops_a_station_once_all_its_platforms_go() -> None:
    """Třída Míru and 14 others are in the register but serve nothing."""
    stops = [_station("S7"), _platform("S7P1", "S7"), _station("S8"), _platform("S8P1", "S8")]
    times = [StopTime("t", "08:00:00", "08:00:00", "S8P1", 0, 0, 0)]

    assert {s.stop_id for s in prune_unserved_stops(stops, times)} == {"S8", "S8P1"}


def test_pruning_keeps_everything_that_is_served() -> None:
    stops = [_station("S1"), _platform("S1P1", "S1"), _platform("S1P2", "S1")]
    times = [
        StopTime("t", "08:00:00", "08:00:00", "S1P1", 0, 0, 0),
        StopTime("t", "09:00:00", "09:00:00", "S1P2", 1, 0, 0),
    ]
    assert len(prune_unserved_stops(stops, times)) == 3


# --- ids --------------------------------------------------------------------


def test_ids_are_stable_and_unambiguous() -> None:
    assert stop_id(16, 2) == "S16P2"
    assert trip_id(9, 115) == "L9C115"
    # The upstream's own encoding would collide here; explicit ids do not.
    assert stop_id(1, 60) != stop_id(16, 0)
