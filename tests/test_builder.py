import logging

import pytest

from dpmp_gtfs.api.models import Connection, ConnectionStop, Line
from dpmp_gtfs.api.models import Stop as ApiStop
from dpmp_gtfs.ids import route_id, stop_id, trip_id
from dpmp_gtfs.static.builder import (
    ROUTE_TYPE_BUS,
    ROUTE_TYPE_TROLLEYBUS,
    build_stops,
    build_trips_and_stop_times,
    prune_unserved_stops,
    stop_seconds,
)
from dpmp_gtfs.timeutil import format_gtfs_time
from dpmp_gtfs.types import Stop, StopTime, Timetable
from dpmp_gtfs.upstream import TROLLEYBUS_LINES


def _stop(departure: str, *, stop: int = 1, platform: str = "1") -> ConnectionStop:
    return ConnectionStop.model_validate(
        {"stopId": stop, "platformId": platform, "departureTime": departure}
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
    stops = [_stop("04:25:00"), _stop("04:30:00"), _stop("04:42:00")]
    assert stop_seconds(stops) == [15900, 16200, 16920]


def test_a_trip_crossing_midnight_keeps_counting_past_24h() -> None:
    """Line 3 trips 322/324 run 23:55 -> 00:23.

    Wrapping to 00:23 would describe a trip that arrives 23 hours before it
    departs, which consumers reject or render as an all-day journey.
    """
    stops = [_stop("23:55:00"), _stop("23:58:00"), _stop("00:02:00"), _stop("00:23:00")]
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
    seconds = stop_seconds([_stop("23:58:00"), _stop("00:52:00")])
    assert [format_gtfs_time(s) for s in seconds] == ["23:58:00", "24:52:00"]


def test_night_trips_starting_after_midnight_are_left_alone() -> None:
    """Lines 98/99 have trips departing at 00:05 etc.

    Every one of them runs daily (codes 2+4+5), so which service day they are
    assigned to cannot change the feed -- and treating them as ordinary
    early-morning times is the simpler reading.
    """
    seconds = stop_seconds([_stop("00:05:00"), _stop("00:30:00"), _stop("00:50:00")])
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

    kept, unserved = prune_unserved_stops(stops, times)
    assert {s.stop_id for s in kept} == {"S7", "S7P1"}
    assert set(unserved) == {"S7P2"}


def test_pruning_drops_a_station_once_all_its_platforms_go() -> None:
    """Třída Míru and 14 others are in the register but serve nothing."""
    stops = [_station("S7"), _platform("S7P1", "S7"), _station("S8"), _platform("S8P1", "S8")]
    times = [StopTime("t", "08:00:00", "08:00:00", "S8P1", 0, 0, 0)]

    kept, unserved = prune_unserved_stops(stops, times)
    assert {s.stop_id for s in kept} == {"S8", "S8P1"}
    assert set(unserved) == {"S7", "S7P1"}


def test_pruning_keeps_everything_that_is_served() -> None:
    stops = [_station("S1"), _platform("S1P1", "S1"), _platform("S1P2", "S1")]
    times = [
        StopTime("t", "08:00:00", "08:00:00", "S1P1", 0, 0, 0),
        StopTime("t", "09:00:00", "09:00:00", "S1P2", 1, 0, 0),
    ]
    kept, unserved = prune_unserved_stops(stops, times)
    assert len(kept) == 3
    assert unserved == {}


def test_dropped_stops_are_reported_not_discarded() -> None:
    """Losing service may mean a diversion rather than a closure, so the
    caller has to be able to see which stops went."""
    stops = [_station("S7"), _platform("S7P1", "S7")]
    _, unserved = prune_unserved_stops(stops, [])
    assert set(unserved) == {"S7", "S7P1"}


# --- ids --------------------------------------------------------------------


def test_ids_are_stable_and_unambiguous() -> None:
    assert stop_id(16, 2) == "S16P2"
    assert route_id("9") == "L9"
    assert trip_id("9", 115) == "L9C115"
    # The upstream's own encoding would collide here; explicit ids do not.
    assert stop_id(1, 60) != stop_id(16, 0)


def test_a_feed_with_no_services_is_refused_with_a_usable_message() -> None:
    """Regression: this died on ``IndexError: list index out of range`` deep in
    the CSV writer, which the scheduler then reported as the cause of a failed
    rebuild. The actual condition -- an empty crawl -- said nothing about it."""
    from dpmp_gtfs.exceptions import FeedBuildError
    from dpmp_gtfs.static.writer import feed_to_files
    from dpmp_gtfs.types import Feed

    with pytest.raises(FeedBuildError, match="no services"):
        feed_to_files(Feed())


# --- stops, routes and trips from a crawled timetable ------------------------


def test_platforms_inherit_the_station_position(simple_timetable: Timetable) -> None:
    stops = build_stops(simple_timetable)
    parent = next(s for s in stops if s.stop_id == "S1")
    child = next(s for s in stops if s.stop_id == "S1P1")

    assert (child.stop_lat, child.stop_lon) == (parent.stop_lat, parent.stop_lon)
    assert child.platform_code == "1"
    assert child.parent_station == "S1"


def test_wheelchair_boarding_comes_from_the_stop_fixed_codes(
    simple_timetable: Timetable,
) -> None:
    stops = build_stops(simple_timetable)
    assert next(s for s in stops if s.stop_id == "S1").wheelchair_boarding == 1


def test_direction_comes_from_the_timetable_not_the_stop_order(
    simple_timetable: Timetable,
) -> None:
    trips, _, _ = build_trips_and_stop_times(simple_timetable)
    assert {t.trip_id: t.direction_id for t in trips} == {"L1C1": 0, "L1C2": 1}


# --- stops with no coordinates -----------------------------------------------


def test_the_real_stops_payload_validates_and_drops_the_coordinateless_stop(
    stops_payload: list[dict],
) -> None:
    """Regression: stop 147 ("Opočínek,rozvodna") has no gpsLat/gpsLon at all in
    the real /stops response. One such record must not fail validation for the
    whole payload, and build_stops must skip it rather than publish a stop with
    no position -- while every other stop in the payload survives untouched."""
    stops = [ApiStop.model_validate(s) for s in stops_payload]
    assert len(stops) == len(stops_payload) == 219

    timetable = Timetable(stops=stops, lines=[])
    built = build_stops(timetable)

    assert "S147" not in {s.stop_id for s in built}
    # One parent Stop per surviving api stop; none of them had platforms
    # (there are no connections here), so this is also the total count.
    assert len(built) == len(stops) - 1


def test_a_stop_time_referencing_a_coordinateless_stop_is_dropped() -> None:
    stops = [
        ApiStop.model_validate({"id": 1, "name": "A", "gpsLat": 50.0, "gpsLon": 15.0}),
        ApiStop.model_validate({"id": 147, "name": "Opočínek,rozvodna"}),
        ApiStop.model_validate({"id": 2, "name": "B", "gpsLat": 50.02, "gpsLon": 15.02}),
    ]
    lines = [Line.model_validate({"id": "1", "jdfId": "655001"})]
    connection = Connection.model_validate(
        {
            "lineId": "1",
            "connectionId": 1,
            "fixedCodes": ["X", "@"],
            "stops": [
                {"stopId": 1, "platformId": "1", "departureTime": "04:00:00"},
                {"stopId": 147, "platformId": "1", "departureTime": "04:05:00"},
                {"stopId": 2, "platformId": "1", "departureTime": "04:10:00"},
            ],
        }
    )
    timetable = Timetable(stops=stops, lines=lines, connections={("1", 1): connection})

    stop_ids = {s.stop_id for s in build_stops(timetable)}
    trips, stop_times, _ = build_trips_and_stop_times(timetable)

    assert "S147" not in stop_ids
    assert all(st.stop_id in stop_ids for st in stop_times)
    assert [st.stop_id for st in stop_times] == ["S1P1", "S2P1"]
    assert len(trips) == 1


# --- trips left with too few stops -------------------------------------------


def test_a_trip_with_fewer_than_two_usable_stops_is_dropped() -> None:
    stops = [
        ApiStop.model_validate({"id": 1, "name": "A", "gpsLat": 50.0, "gpsLon": 15.0}),
        ApiStop.model_validate({"id": 2, "name": "B", "gpsLat": 50.01, "gpsLon": 15.01}),
    ]
    lines = [Line.model_validate({"id": "1", "jdfId": "655001"})]
    connection = Connection.model_validate(
        {
            "lineId": "1",
            "connectionId": 1,
            "fixedCodes": ["X", "@"],
            "stops": [
                {"stopId": 1, "platformId": "1", "departureTime": "04:00:00"},
                {"stopId": 2, "platformId": "", "departureTime": "04:10:00"},
            ],
        }
    )
    timetable = Timetable(stops=stops, lines=lines, connections={("1", 1): connection})

    trips, stop_times, _ = build_trips_and_stop_times(timetable)
    assert trips == []
    assert stop_times == []


def test_trip_headsign_comes_from_the_last_surviving_stop() -> None:
    """The naive ``connection.stops[-1]`` would read "Nowhere" here, even though
    that stop was dropped for lacking a numeric platform."""
    stops = [
        ApiStop.model_validate({"id": 1, "name": "Start", "gpsLat": 50.0, "gpsLon": 15.0}),
        ApiStop.model_validate({"id": 2, "name": "Middle", "gpsLat": 50.01, "gpsLon": 15.01}),
        ApiStop.model_validate({"id": 3, "name": "Nowhere", "gpsLat": 50.02, "gpsLon": 15.02}),
    ]
    lines = [Line.model_validate({"id": "1", "jdfId": "655001"})]
    connection = Connection.model_validate(
        {
            "lineId": "1",
            "connectionId": 1,
            "fixedCodes": ["X", "@"],
            "stops": [
                {"stopId": 1, "platformId": "1", "departureTime": "04:00:00"},
                {"stopId": 2, "platformId": "1", "departureTime": "04:05:00"},
                {"stopId": 3, "platformId": "", "departureTime": "04:10:00"},
            ],
        }
    )
    timetable = Timetable(stops=stops, lines=lines, connections={("1", 1): connection})

    trips, stop_times, _ = build_trips_and_stop_times(timetable)
    assert len(trips) == 1
    assert trips[0].trip_headsign == "Middle"
    assert len(stop_times) == 2


# --- diagnostics --------------------------------------------------------------


def test_a_stop_unknown_to_stops_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A station timetables reference but /stops never listed at all -- distinct
    from one /stops lists without coordinates."""
    lines = [Line.model_validate({"id": "1", "jdfId": "655001"})]
    connection = Connection.model_validate(
        {
            "lineId": "1",
            "connectionId": 1,
            "fixedCodes": ["X", "@"],
            "stops": [
                {"stopId": 999, "platformId": "1", "departureTime": "04:00:00"},
                {"stopId": 998, "platformId": "1", "departureTime": "04:10:00"},
            ],
        }
    )
    timetable = Timetable(stops=[], lines=lines, connections={("1", 1): connection})

    with caplog.at_level(logging.ERROR):
        build_stops(timetable)

    assert "999" in caplog.text
    assert "998" in caplog.text
