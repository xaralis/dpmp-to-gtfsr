"""Tests for the readable vehicle view.

This is the join GTFS-RT deliberately leaves to the consumer: stop ids turned
back into names and times. Doing it on the server means one answer to "which
stop is next", so these check that answer rather than the plumbing.
"""

import datetime as dt
from typing import Any

import pytest

from dpmp_gtfs.api.models import Vehicle
from dpmp_gtfs.realtime.index import ScheduledStop, ScheduledTrip, StaticIndex
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


def _vehicle(**over: Any) -> Vehicle:
    base: dict[str, object] = {
        "vid": "105",
        "lineId": "9",
        "connectionId": 115,
        "gpsLat": 50.03,
        "gpsLon": 15.77,
        "destinationName": "Spojil,točna",
        "currentDelay": None,
        "onStation": False,
    }
    base.update(over)
    return Vehicle.model_validate(base)


def _view(vehicle: Vehicle) -> Any:
    index = _index()
    views = build_vehicle_views([vehicle], index, NOW)
    return views[0]


# --- surrounding stops ------------------------------------------------------


def test_next_stop_is_the_one_being_approached() -> None:
    v = _view(_vehicle(nextStopId=2, nextStopPlatformId=1))
    assert v.next_stop.id == "S2P1"
    assert v.next_stop.name == "Masarykovo nám."
    assert v.next_stop.scheduled == "19:05"


def test_previous_stop_is_the_one_already_served() -> None:
    v = _view(_vehicle(nextStopId=2, nextStopPlatformId=1))
    assert v.previous_stop.id == "S1P1"
    assert v.previous_stop.name == "Hlavní nádraží"


def test_a_vehicle_at_the_first_stop_has_no_previous() -> None:
    """It has not been anywhere yet; inventing a stop would be a lie."""
    v = _view(_vehicle(nextStopId=1, nextStopPlatformId=1))
    assert v.previous_stop is None
    assert v.next_stop.id == "S1P1"


def test_stop_position_is_reported_for_progress() -> None:
    v = _view(_vehicle(nextStopId=3, nextStopPlatformId=1))
    assert (v.stop_index, v.stops_total) == (2, 3)


def test_an_unknown_platform_falls_back_to_the_station() -> None:
    """A vehicle can report a platform the trip does not use while the station
    itself is on the route; losing the vehicle over that would be worse than
    naming the station."""
    v = _view(_vehicle(nextStopId=2, nextStopPlatformId=9))  # station 2, platform 9
    assert v.next_stop.id == "S2P1"


def test_a_vehicle_with_no_next_stop_still_produces_a_view() -> None:
    """The upstream can decline to name a next stop at all; that must not be
    confused with the vehicle running off the trip entirely."""
    v = _view(_vehicle(nextStopId=None, nextStopPlatformId=None))
    assert v.previous_stop is None
    assert v.next_stop is None
    assert v.stop_index is None


# --- delay --------------------------------------------------------------


def test_delay_is_none_when_the_upstream_reports_none() -> None:
    v = _view(_vehicle(currentDelay=None))
    assert v.delay_seconds is None
    assert v.delay_measured is False


def test_a_reported_delay_is_marked_as_measured() -> None:
    v = _view(_vehicle(currentDelay="PT2M0S"))
    assert v.delay_seconds == 120
    assert v.delay_measured is True


def test_an_unparseable_delay_shows_as_zero_not_a_raise() -> None:
    """Regression: ``build_vehicle_views`` reads ``vehicle.delay`` on the same
    path ``build_feed_message`` does; a garbage ``currentDelay`` must not take
    the whole view-building loop down either. Zero, not ``None``, per the
    same product decision as the realtime feed."""
    v = _view(_vehicle(currentDelay="n/a"))
    assert v.delay_seconds == 0
    assert v.delay_measured is True


def test_an_early_vehicle_has_a_negative_delay() -> None:
    v = _view(_vehicle(currentDelay="-PT1M0S"))
    assert v.delay_seconds == -60
    assert v.delay_measured is True


# --- classification and payload ---------------------------------------------


def test_trolleybus_lines_are_flagged() -> None:
    assert _view(_vehicle(lineId="9", connectionId=115)).trolleybus is False


def test_a_lettered_line_is_not_mistaken_for_a_trolleybus() -> None:
    """``line_id`` is not guaranteed to be numeric; a line the upstream names
    with letters is simply not a trolleybus rather than a crash."""
    index = StaticIndex({"LX1C1": ScheduledTrip(trip_id="LX1C1", route_id="LX1", stops=())})
    views = build_vehicle_views(
        [_vehicle(lineId="X1", connectionId=1)], index, NOW
    )
    assert views[0].trolleybus is False


def test_reported_at_is_local_time() -> None:
    """The snapshot time is UTC; a passenger-facing timestamp should not be."""
    v = _view(_vehicle())
    assert v.reported_at.startswith("2026-08-07T19:00")


def test_vehicles_off_the_static_feed_are_skipped() -> None:
    index = _index()
    views = build_vehicle_views([_vehicle(connectionId=9999)], index, NOW)
    assert views == []


def test_payload_is_json_ready() -> None:
    import json

    index = _index()
    views = build_vehicle_views([_vehicle(nextStopId=2, nextStopPlatformId=1)], index, NOW)
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
