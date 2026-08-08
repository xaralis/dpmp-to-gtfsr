"""Tests for the readable vehicle view.

This is the join GTFS-RT deliberately leaves to the consumer: stop ids turned
back into names and times. Doing it on the server means one answer to "which
stop is next", so these check that answer rather than the plumbing.
"""

import datetime as dt
from typing import Any

import pytest

from dpmp_gtfs.api.models import Bus
from dpmp_gtfs.realtime.index import ScheduledStop, ScheduledTrip, StaticIndex
from dpmp_gtfs.realtime.tracker import DelayTracker
from dpmp_gtfs.realtime.view import as_payload, build_vehicle_views

NOW = dt.datetime(2026, 8, 7, 17, 0, tzinfo=dt.UTC)  # 19:00 Prague


def _index() -> StaticIndex:
    trip = ScheduledTrip(
        trip_id="L9C115",
        route_id="L9",
        stops=(
            ScheduledStop("S1P1", 1, 0, 19 * 3600, "Hlavní nádraží"),
            ScheduledStop("S2P1", 2, 1, 19 * 3600 + 300, "Masarykovo nám."),
            ScheduledStop("S3P1", 3, 2, 19 * 3600 + 600, "Náměstí Republiky"),
        ),
    )
    return StaticIndex({"L9C115": trip})


def _bus(**overrides: Any) -> Bus:
    base = {
        "vid": "105",
        "state_dtime": "2026-08-07 17:00:00",
        "line_name": "9",
        "line_direction": "S12",
        "destination_name": "Spojil,točna",
        "last_stop_number": "0",
        "last_stop_name": None,
        "current_stop_number": "201",  # station 2, platform 1
        "current_stop_name": "x",
        "current_stop_scheduled_departure": "19:05:00",
        "time_difference": None,
        "connection_no": 115,
        "gps_latitude": 50.03,
        "gps_longitude": 15.77,
    }
    return Bus.model_validate(base | overrides)


def _view(bus: Bus, tracker: DelayTracker | None = None) -> Any:
    index = _index()
    views = build_vehicle_views([bus], index, tracker or DelayTracker(index), NOW)
    return views[0]


# --- surrounding stops ------------------------------------------------------


def test_next_stop_is_the_one_being_approached() -> None:
    v = _view(_bus(current_stop_number="201"))
    assert v.next_stop.id == "S2P1"
    assert v.next_stop.name == "Masarykovo nám."
    assert v.next_stop.scheduled == "19:05"


def test_previous_stop_is_the_one_already_served() -> None:
    v = _view(_bus(current_stop_number="201"))
    assert v.previous_stop.id == "S1P1"
    assert v.previous_stop.name == "Hlavní nádraží"


def test_a_vehicle_at_the_first_stop_has_no_previous() -> None:
    """It has not been anywhere yet; inventing a stop would be a lie."""
    v = _view(_bus(current_stop_number="101"))
    assert v.previous_stop is None
    assert v.next_stop.id == "S1P1"


def test_stop_position_is_reported_for_progress() -> None:
    v = _view(_bus(current_stop_number="301"))
    assert (v.stop_index, v.stops_total) == (2, 3)


def test_an_unknown_platform_falls_back_to_the_station() -> None:
    """A vehicle can report a platform the trip does not use while the station
    itself is on the route; losing the vehicle over that would be worse than
    naming the station."""
    v = _view(_bus(current_stop_number="209"))  # station 2, platform 9
    assert v.next_stop.id == "S2P1"


# --- delay ------------------------------------------------------------------


def test_delay_is_none_when_nothing_supports_a_claim() -> None:
    v = _view(_bus(time_difference=None))
    assert v.delay_seconds is None
    assert v.delay_measured is False


def test_measured_delay_is_marked_as_such() -> None:
    index = _index()
    tracker = DelayTracker(index)
    tracker.observe([_bus(last_stop_number="0")])
    moved = _bus(state_dtime="2026-08-07 17:02:00", last_stop_number="1")
    tracker.observe([moved])

    views = build_vehicle_views([moved], index, tracker, NOW)
    assert views[0].delay_seconds == 120
    assert views[0].delay_measured is True


def test_countdown_fallback_is_not_marked_as_measured() -> None:
    """The distinction matters: the fallback can only ever prove lateness."""
    v = _view(_bus(time_difference="-00:01:00"))
    assert v.delay_seconds == 60
    assert v.delay_measured is False


# --- classification and payload ---------------------------------------------


def test_trolleybus_lines_are_flagged() -> None:
    assert _view(_bus(line_name="9", connection_no=115)).trolleybus is False


def test_reported_at_is_local_time() -> None:
    """state_dtime is UTC; a passenger-facing timestamp should not be."""
    v = _view(_bus(state_dtime="2026-08-07 17:00:00"))
    assert v.reported_at.startswith("2026-08-07T19:00")


def test_vehicles_off_the_static_feed_are_skipped() -> None:
    index = _index()
    views = build_vehicle_views([_bus(connection_no=9999)], index, DelayTracker(index), NOW)
    assert views == []


def test_payload_is_json_ready() -> None:
    import json

    index = _index()
    views = build_vehicle_views([_bus()], index, DelayTracker(index), NOW)
    payload = as_payload(views, NOW)

    assert payload["count"] == 1
    restored = json.loads(json.dumps(payload))
    assert restored["vehicles"][0]["next_stop"]["name"] == "Masarykovo nám."


# --- direction of travel -----------------------------------------------------


def test_bearing_matches_the_compass() -> None:
    """Degrees clockwise from north, which is what CSS rotate() expects."""
    from dpmp_gtfs.realtime.view import _bearing

    assert _bearing((50.0, 15.0), (51.0, 15.0)) == pytest.approx(0, abs=1)
    assert _bearing((50.0, 15.0), (50.0, 16.0)) == pytest.approx(90, abs=1)
    assert _bearing((50.0, 15.0), (49.0, 15.0)) == pytest.approx(180, abs=1)
    assert _bearing((50.0, 15.0), (50.0, 14.0)) == pytest.approx(270, abs=1)


def test_no_bearing_without_somewhere_to_point() -> None:
    """A vehicle at its final stop has no next stop, and one sitting exactly on
    a stop's coordinates gives no direction either."""
    from dpmp_gtfs.realtime.view import _bearing

    assert _bearing((50.0, 15.0), None) is None
    assert _bearing((50.0, 15.0), (50.0, 15.0)) is None
