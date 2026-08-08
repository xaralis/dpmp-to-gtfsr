import datetime as dt
from typing import Any

from google.transit import gtfs_realtime_pb2 as rt

from dpmp_gtfs.api.models import Bus
from dpmp_gtfs.realtime.feed import build_feed_message
from dpmp_gtfs.realtime.index import ScheduledStop, ScheduledTrip, StaticIndex
from dpmp_gtfs.realtime.tracker import DelayTracker


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


def _bus(**overrides: Any) -> Bus:
    base = {
        "vid": "105",
        "state_dtime": "2026-08-07 17:00:00",
        "line_name": "9",
        "line_direction": "S12",
        "destination_name": "x",
        "last_stop_number": "0",
        "last_stop_name": None,
        "current_stop_number": "201",  # station 2, platform 1
        "current_stop_name": "x",
        "current_stop_scheduled_departure": "19:05:00",
        "time_difference": None,
        "connection_no": 115,
        "gps_latitude": 50.03,
        "gps_longitude": 15.77,
        "gps_course": 271.0,
    }
    return Bus.model_validate(base | overrides)


NOW = dt.datetime(2026, 8, 7, 17, 0, tzinfo=dt.UTC)


def test_header_is_a_full_dataset() -> None:
    msg = build_feed_message([], _index(), DelayTracker(_index()), NOW)
    assert msg.header.gtfs_realtime_version == "2.0"
    assert msg.header.incrementality == rt.FeedHeader.FULL_DATASET
    assert msg.header.timestamp == int(NOW.timestamp())


def test_a_vehicle_produces_a_position() -> None:
    index = _index()
    msg = build_feed_message([_bus()], index, DelayTracker(index), NOW)

    positions = [e.vehicle for e in msg.entity if e.HasField("vehicle")]
    assert len(positions) == 1
    vp = positions[0]
    assert vp.vehicle.id == "105"
    assert vp.trip.trip_id == "L9C115"
    assert vp.trip.route_id == "L9"
    assert vp.stop_id == "S2P1"
    assert vp.current_stop_sequence == 1
    assert round(vp.position.bearing) == 271


def test_trip_descriptor_carries_start_date_and_time() -> None:
    index = _index()
    msg = build_feed_message([_bus()], index, DelayTracker(index), NOW)
    trip = next(e.vehicle.trip for e in msg.entity if e.HasField("vehicle"))
    assert trip.start_date == "20260807"
    assert trip.start_time == "19:00:00"


def test_no_trip_update_without_delay_evidence() -> None:
    """A vehicle that has not departed has no delay, and must not be described
    as running exactly on time."""
    index = _index()
    msg = build_feed_message([_bus(time_difference=None)], index, DelayTracker(index), NOW)

    assert any(e.HasField("vehicle") for e in msg.entity), "position is still published"
    assert not any(e.HasField("trip_update") for e in msg.entity)


def test_trip_update_predicts_only_the_remaining_stops() -> None:
    index = _index()
    tracker = DelayTracker(index)
    tracker.observe([_bus(last_stop_number="0")])
    moved = _bus(state_dtime="2026-08-07 17:02:00", last_stop_number="1")
    tracker.observe([moved])

    msg = build_feed_message([moved], index, tracker, NOW)
    update = next(e.trip_update for e in msg.entity if e.HasField("trip_update"))

    assert update.delay == 120
    # Vehicle is at S2P1 (sequence 1), so sequences 1 and 2 remain.
    assert [s.stop_sequence for s in update.stop_time_update] == [1, 2]
    assert all(s.arrival.delay == 120 for s in update.stop_time_update)


def test_vehicles_missing_from_the_static_feed_are_skipped() -> None:
    index = _index()
    msg = build_feed_message([_bus(connection_no=9999)], index, DelayTracker(index), NOW)
    assert len(msg.entity) == 0


def test_entity_ids_are_unique() -> None:
    index = _index()
    tracker = DelayTracker(index)
    tracker.observe([_bus(last_stop_number="0")])
    moved = _bus(state_dtime="2026-08-07 17:02:00", last_stop_number="1")
    tracker.observe([moved])

    msg = build_feed_message([moved], index, tracker, NOW)
    ids = [e.id for e in msg.entity]
    assert len(ids) == len(set(ids))


def test_feed_round_trips_through_protobuf() -> None:
    index = _index()
    tracker = DelayTracker(index)
    tracker.observe([_bus(last_stop_number="0")])
    # Departed 50s early -- negative delays must survive serialisation.
    early = _bus(state_dtime="2026-08-07 16:59:10", last_stop_number="1")
    tracker.observe([early])

    msg = build_feed_message([early], index, tracker, NOW)
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
    bus = _bus(current_stop_number="209", last_stop_number="1", time_difference="-00:02:00")

    msg = build_feed_message([bus], index, DelayTracker(index), NOW)

    positions = [e.vehicle for e in msg.entity if e.HasField("vehicle")]
    assert positions[0].current_stop_sequence == 1

    updates = [e.trip_update for e in msg.entity if e.HasField("trip_update")]
    assert [u.stop_id for u in updates[0].stop_time_update] == ["S2P1", "S3P1"]


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
    bus = _bus(
        line_name="98",
        connection_no=9,
        current_stop_number="201",
        state_dtime="2026-08-08 22:30:00",
    )

    msg = build_feed_message([bus], index, DelayTracker(index), NOW)

    positions = [e.vehicle for e in msg.entity if e.HasField("vehicle")]
    assert positions[0].trip.start_date == "20260808"
    assert positions[0].trip.start_time == "23:58:00"


def test_one_malformed_vehicle_does_not_discard_the_snapshot() -> None:
    """Regression: ``int(line_name)`` raised out of the middle of the loop.

    Nothing guarantees the upstream's numeric fields hold numbers. A single
    vehicle with a lettered line took all fifty others down with it, and since
    the scheduler keeps the last good feed on failure, the realtime feed then
    stayed frozen for as long as that vehicle was reported.
    """
    index = _index()
    good = _bus(vid="1")
    lettered = _bus(vid="2", line_name="X1")
    garbled = _bus(vid="3", current_stop_number="n/a")

    msg = build_feed_message([good, lettered, garbled], index, DelayTracker(index), NOW)

    # The good vehicle is published; the one with an unusable line is skipped.
    assert [e.vehicle.vehicle.id for e in msg.entity if e.HasField("vehicle")] == ["1", "3"]


def test_a_malformed_vehicle_does_not_break_the_tracker_either() -> None:
    index = _index()
    tracker = DelayTracker(index)
    assert tracker.observe([_bus(vid="2", line_name="X1")]) == 0
