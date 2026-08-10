import datetime as dt
from typing import Any

from google.transit import gtfs_realtime_pb2 as rt

from dpmp_gtfs.api.models import Vehicle
from dpmp_gtfs.realtime.feed import build_feed_message
from dpmp_gtfs.realtime.index import ScheduledStop, ScheduledTrip, StaticIndex


def _index() -> StaticIndex:
    trip = ScheduledTrip(
        trip_id="L9C115",
        route_id="L9",
        stops=(
            ScheduledStop("S1P1", 1, 0, 19 * 3600),
            ScheduledStop("S2P1", 2, 1, 19 * 3600 + 300),
            ScheduledStop("S3P1", 3, 2, 19 * 3600 + 600),
        ),
    )
    return StaticIndex({"L9C115": trip})


def _vehicle(**over: object) -> Vehicle:
    payload: dict[str, object] = {
        "vid": "100",
        "lineId": "1",
        "connectionId": 1,
        "gpsLat": 50.01,
        "gpsLon": 15.77,
        "currentDelay": "PT0S",
        "onStation": False,
    }
    payload.update(over)
    return Vehicle.model_validate(payload)


def _line9(**over: Any) -> Vehicle:
    """A vehicle running the ``L9C115`` trip that :func:`_index` knows."""
    base: dict[str, object] = {
        "vid": "105",
        "lineId": "9",
        "connectionId": 115,
        "gpsLat": 50.03,
        "gpsLon": 15.77,
        "currentDelay": None,
        "onStation": False,
    }
    base.update(over)
    return _vehicle(**base)


NOW = dt.datetime(2026, 8, 7, 17, 0, tzinfo=dt.UTC)  # 19:00 Prague


def test_header_is_a_full_dataset() -> None:
    msg = build_feed_message([], _index(), NOW)
    assert msg.header.gtfs_realtime_version == "2.0"
    assert msg.header.incrementality == rt.FeedHeader.FULL_DATASET
    assert msg.header.timestamp == int(NOW.timestamp())


def test_a_vehicle_produces_a_position() -> None:
    index = _index()
    vehicle = _line9(nextStopId=2, nextStopPlatformId=1)
    msg = build_feed_message([vehicle], index, NOW)

    positions = [e.vehicle for e in msg.entity if e.HasField("vehicle")]
    assert len(positions) == 1
    vp = positions[0]
    assert vp.vehicle.id == "105"
    assert vp.trip.trip_id == "L9C115"
    assert vp.trip.route_id == "L9"
    assert vp.stop_id == "S2P1"
    assert vp.current_stop_sequence == 1


def test_trip_descriptor_carries_start_date_and_time() -> None:
    index = _index()
    msg = build_feed_message([_line9(nextStopId=2, nextStopPlatformId=1)], index, NOW)
    trip = next(e.vehicle.trip for e in msg.entity if e.HasField("vehicle"))
    assert trip.start_date == "20260807"
    assert trip.start_time == "19:00:00"


def test_delay_comes_straight_from_the_vehicle(static_index: StaticIndex) -> None:
    vehicle = _vehicle(currentDelay="-PT1M43S")
    message = build_feed_message([vehicle], static_index)

    update = next(e.trip_update for e in message.entity if e.HasField("trip_update"))
    assert update.delay == -103


def test_no_delay_means_no_trip_update(static_index: StaticIndex) -> None:
    vehicle = _vehicle(currentDelay=None)
    message = build_feed_message([vehicle], static_index)

    assert not any(e.HasField("trip_update") for e in message.entity)
    assert any(e.HasField("vehicle") for e in message.entity)


def test_on_station_reports_stopped_at(static_index: StaticIndex) -> None:
    stopped = build_feed_message([_vehicle(onStation=True)], static_index)
    moving = build_feed_message([_vehicle(onStation=False)], static_index)

    assert (
        next(e.vehicle for e in stopped.entity if e.HasField("vehicle")).current_status
        == rt.VehiclePosition.STOPPED_AT
    )
    assert (
        next(e.vehicle for e in moving.entity if e.HasField("vehicle")).current_status
        == rt.VehiclePosition.IN_TRANSIT_TO
    )


def test_trip_update_predicts_the_remaining_stops_with_decay() -> None:
    """Unlike the retired tracker, a projected delay decays: a vehicle that is
    late tends to claw back a little at each further stop."""
    index = _index()
    vehicle = _line9(nextStopId=2, nextStopPlatformId=1, currentDelay="PT2M0S")
    msg = build_feed_message([vehicle], index, NOW)
    update = next(e.trip_update for e in msg.entity if e.HasField("trip_update"))

    assert update.delay == 120
    # Vehicle is at S2P1 (sequence 1), so sequences 1 and 2 remain.
    assert [s.stop_sequence for s in update.stop_time_update] == [1, 2]
    assert [s.arrival.delay for s in update.stop_time_update] == [120, 108]
    assert [s.departure.delay for s in update.stop_time_update] == [120, 108]


def test_vehicles_missing_from_the_static_feed_are_skipped() -> None:
    index = _index()
    msg = build_feed_message([_line9(connectionId=9999)], index, NOW)
    assert len(msg.entity) == 0


def test_entity_ids_are_unique() -> None:
    index = _index()
    vehicle = _line9(nextStopId=2, nextStopPlatformId=1, currentDelay="PT2M0S")
    msg = build_feed_message([vehicle], index, NOW)
    ids = [e.id for e in msg.entity]
    assert len(ids) == len(set(ids))


def test_feed_round_trips_through_protobuf() -> None:
    index = _index()
    # 50s early -- negative delays must survive serialisation.
    vehicle = _line9(nextStopId=2, nextStopPlatformId=1, currentDelay="-PT50S")

    msg = build_feed_message([vehicle], index, NOW)
    restored = rt.FeedMessage()
    restored.ParseFromString(msg.SerializeToString())

    update = next(e.trip_update for e in restored.entity if e.HasField("trip_update"))
    assert update.delay == -50


# --- regressions ------------------------------------------------------------


def test_a_vehicle_on_an_unlisted_platform_still_gets_predictions() -> None:
    """Regression: the feed used to match the vehicle's stop id exactly.

    Vehicles do report a platform their trip does not call at while the station
    itself is plainly on the route. Matching on the id alone left those trip
    updates with no stop_time_update at all and their positions with no
    current_stop_sequence -- while the map page, which had the station
    fallback, showed them correctly. The two must not disagree.
    """
    index = _index()
    # Station 2 is on the trip, but as platform 1; the vehicle claims 9.
    vehicle = _line9(nextStopId=2, nextStopPlatformId=9, currentDelay="-PT2M")

    msg = build_feed_message([vehicle], index, NOW)

    positions = [e.vehicle for e in msg.entity if e.HasField("vehicle")]
    assert positions[0].current_stop_sequence == 1

    updates = [e.trip_update for e in msg.entity if e.HasField("trip_update")]
    assert [u.stop_id for u in updates[0].stop_time_update] == ["S2P1", "S3P1"]


def test_an_unknown_platform_falls_back_to_the_parent_station_id() -> None:
    """Regression: when the upstream does not name a platform at all, the
    published stop_id must still resolve against the static feed -- so it is
    the parent station, not an empty guess or a fabricated platform."""
    index = _index()
    vehicle = _line9(nextStopId=2, nextStopPlatformId=None)

    msg = build_feed_message([vehicle], index, NOW)

    position = next(e.vehicle for e in msg.entity if e.HasField("vehicle"))
    assert position.stop_id == "S2"
    assert position.current_stop_sequence == 1


def test_the_unknown_platform_fallback_resolves_in_a_real_feed(static_index: StaticIndex) -> None:
    """The parent-station id the unknown-platform fallback publishes must
    actually appear in a built static feed, not just satisfy the in-memory
    :class:`ScheduledTrip` check that the other tests use."""
    vehicle = _vehicle(nextStopId=1, nextStopPlatformId=None)
    msg = build_feed_message([vehicle], static_index)

    position = next(e.vehicle for e in msg.entity if e.HasField("vehicle"))
    assert position.stop_id == "S1"
    assert static_index.stop_name(position.stop_id) != ""


def test_a_trip_running_past_midnight_reports_the_day_it_started() -> None:
    """Regression: start_date came from the schedule alone, so a trip that
    departs at 23:58 and is still running at 00:30 was reported against the new
    calendar day. Consumers key TripUpdates on (trip_id, start_date)."""
    late = ScheduledTrip(
        trip_id="L98C9",
        route_id="L98",
        stops=(
            ScheduledStop("S1P1", 1, 0, 23 * 3600 + 58 * 60),
            ScheduledStop("S2P1", 2, 1, 24 * 3600 + 52 * 60),
        ),
    )
    index = StaticIndex({"L98C9": late})
    # 00:30 Prague on 9 August = 22:30 UTC on the 8th.
    vehicle = _vehicle(lineId="98", connectionId=9, nextStopId=1, nextStopPlatformId=1)
    now = dt.datetime(2026, 8, 8, 22, 30, tzinfo=dt.UTC)

    msg = build_feed_message([vehicle], index, now)

    positions = [e.vehicle for e in msg.entity if e.HasField("vehicle")]
    assert positions[0].trip.start_date == "20260808"
    assert positions[0].trip.start_time == "23:58:00"
