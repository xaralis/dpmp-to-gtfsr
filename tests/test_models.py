import datetime as dt

import pytest

from dpmp_gtfs.api.models import (
    Connection,
    Line,
    Stop,
    Vehicle,
    VehiclesResponse,
    parse_iso_duration,
)


def test_vehicles_parse(vehicles_payload):
    payload = VehiclesResponse.model_validate(vehicles_payload)
    assert payload.vehicles
    v = payload.vehicles[0]
    assert v.vid
    assert isinstance(v.gps_latitude, float)


def test_delay_is_a_real_signed_duration():
    assert parse_iso_duration("-PT1M43S") == dt.timedelta(seconds=-103)
    assert parse_iso_duration("PT2M") == dt.timedelta(minutes=2)
    assert parse_iso_duration("PT0S") == dt.timedelta(0)
    assert parse_iso_duration("-PT1H0M5S") == dt.timedelta(seconds=-3605)


def test_delay_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_iso_duration("1M43S")


def _vehicle(**over: object) -> Vehicle:
    payload: dict[str, object] = {
        "vid": "1",
        "lineId": "9",
        "connectionId": 1,
        "gpsLat": 50.0,
        "gpsLon": 15.0,
    }
    payload.update(over)
    return Vehicle.model_validate(payload)


def test_an_unparseable_delay_is_zero_not_a_raise():
    """``currentDelay`` is typed as a plain ``str`` by the upstream, so a
    garbage value passes pydantic validation. ``Vehicle.delay`` must absorb
    that rather than raise, or one bad vehicle takes the whole snapshot's
    feed-building loop down with it.

    Zero, not ``None``, is a deliberate product decision: the upstream did
    try to describe this vehicle, just badly, and a trip published with no
    delay beats one that vanishes from the feed. Do not "fix" this back to
    ``None`` -- see the case right below, which is the one that actually
    means "no evidence either way".
    """
    assert _vehicle(currentDelay="n/a").delay == dt.timedelta(0)


def test_an_absent_delay_is_none_not_zero():
    """Absent is not zero: publishing zero would assert punctuality for every
    vehicle the upstream declined to describe. This is the case the previous
    test must not be collapsed into."""
    assert _vehicle(currentDelay=None).delay is None
    assert _vehicle().delay is None


def test_stop_flags_come_from_fixed_codes():
    step_free = Stop.model_validate(
        {"id": 1, "name": "Zkušební", "gpsLat": 50.0, "gpsLon": 15.0, "fixedCodes": ["@"]}
    )
    on_request = Stop.model_validate(
        {"id": 2, "name": "Druhá", "gpsLat": 50.0, "gpsLon": 15.0, "fixedCodes": ["x"]}
    )
    plain = Stop.model_validate({"id": 3, "name": "Třetí", "gpsLat": 50.0, "gpsLon": 15.0})

    assert step_free.step_free and not step_free.on_request
    # Lower-case x is "on request"; upper-case X on a *trip* means weekdays.
    assert on_request.on_request and not on_request.step_free
    assert not plain.step_free and not plain.on_request


def test_a_stop_with_no_coordinates_still_validates():
    """Stop 147, "Opočínek,rozvodna", publishes no gpsLat/gpsLon at all -- see
    tests/fixtures/stops.json. A required field here would fail the whole
    /stops payload over this one record."""
    stop = Stop.model_validate({"id": 147, "name": "Opočínek,rozvodna"})
    assert stop.gps_latitude is None
    assert stop.gps_longitude is None


def test_the_real_stops_payload_all_validate(stops_payload):
    stops = [Stop.model_validate(s) for s in stops_payload]
    assert len(stops) == len(stops_payload) == 219
    assert sum(1 for s in stops if s.gps_latitude is None) == 1


def test_line_exposes_its_jdf_id(lines_payload):
    line = Line.model_validate(lines_payload[0])
    assert line.jdf_id.startswith("655")
    assert line.id


def test_connection_stop_times(connection_payload):
    conn = Connection.model_validate(connection_payload)
    assert conn.stops
    assert conn.stops[0].departure.hour >= 0
    assert conn.line_id
