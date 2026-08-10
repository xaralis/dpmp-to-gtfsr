import datetime as dt

import pytest

from dpmp_gtfs.api.models import Connection, Line, Stop, VehiclesResponse, parse_iso_duration


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


def test_line_exposes_its_jdf_id(lines_payload):
    line = Line.model_validate(lines_payload[0])
    assert line.jdf_id.startswith("655")
    assert line.id


def test_connection_stop_times(connection_payload):
    conn = Connection.model_validate(connection_payload)
    assert conn.stops
    assert conn.stops[0].departure.hour >= 0
    assert conn.line_id
