"""Tests for the API model layer.

Several of these are regressions against real defects in the predecessor
project (xaralis/dpmp-gtfs); those are called out individually.
"""

import datetime as dt

import pytest

from dpmp_gtfs.api.models import Bus, ConnectionDetail, Station, parse_duration, parse_hhmm

# --- duration parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("00:00:00", 0),
        ("00:01:06", 66),
        ("00:03:42", 222),
        ("-00:00:02", -2),
        ("-00:01:24", -84),
        ("-00:03:40", -220),
        ("-01:02:03", -3723),
    ],
)
def test_parse_duration_handles_both_signs(raw: str, seconds: int) -> None:
    assert parse_duration(raw).total_seconds() == seconds


def test_negative_durations_are_not_wrapped_into_a_positive_day() -> None:
    """Regression: the old code used ``timedelta.seconds`` instead of
    ``total_seconds()``. Because ``.seconds`` is always non-negative, -90s came
    out as 86310 -- roughly a day early rather than a minute and a half late.
    Negative values are the common case, not an edge case.
    """
    assert parse_duration("-00:01:30").total_seconds() == -90


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="signed HH:MM:SS"):
        parse_duration("2 minutes")


def test_parse_hhmm() -> None:
    assert parse_hhmm("0425") == dt.time(4, 25)
    assert parse_hhmm("2358") == dt.time(23, 58)


# --- Bus --------------------------------------------------------------------


def _bus(**overrides: object) -> Bus:
    base = {
        "vid": "105",
        "state_dtime": "2026-08-07 17:04:04.831",
        "line_name": "9",
        "line_direction": "S12",
        "destination_name": "Spojil,točna",
        "last_stop_number": "14",
        "last_stop_name": "Krajský úřad",
        "current_stop_number": "8301",
        "current_stop_name": "Zlatá štika",
        "current_stop_scheduled_departure": "18:58:00",
        "time_difference": "-00:01:48",
        "connection_no": 115,
        "gps_latitude": 50.039833,
        "gps_longitude": 15.784618,
    }
    return Bus.model_validate(base | overrides)


def test_state_dtime_is_read_as_utc() -> None:
    """Regression: the upstream sends naive timestamps that are already UTC.

    The old code called ``.astimezone(prague_tz)`` on the naive value, which
    Python interprets as *local* time -- shifting every vehicle timestamp by the
    UTC offset (two hours in summer).
    """
    bus = _bus(state_dtime="2026-08-07 17:04:04.831")
    assert bus.state_dtime.tzinfo == dt.UTC
    assert bus.state_dtime.hour == 17


def test_current_stop_number_decodes_to_station_and_platform() -> None:
    assert _bus(current_stop_number="1602").current_station == 16
    assert _bus(current_stop_number="1602").current_platform == 2
    assert _bus(current_stop_number="19902").current_station == 199
    assert _bus(current_stop_number="19902").current_platform == 2


def test_last_stop_number_is_a_bare_station_number() -> None:
    """It is *not* encoded like current_stop_number -- no platform component."""
    assert _bus(last_stop_number="53").last_station == 53


def test_last_station_is_none_before_the_vehicle_has_departed() -> None:
    assert _bus(last_stop_number="0", last_stop_name=None).last_station is None


def test_countdown_is_none_when_upstream_sends_null() -> None:
    """A vehicle waiting at its first stop reports no time_difference at all.

    Roughly 42% of vehicles are in this state at any moment. The old code
    substituted a delay of 0, which asserts "running exactly on time" -- a claim
    the data does not support.
    """
    assert _bus(time_difference=None).countdown is None


def test_countdown_parses_signed_values() -> None:
    assert _bus(time_difference="-00:01:48").countdown == dt.timedelta(seconds=-108)
    assert _bus(time_difference="00:01:06").countdown == dt.timedelta(seconds=66)


def test_line_is_numeric() -> None:
    assert _bus(line_name="902").line == 902


# --- real payloads ----------------------------------------------------------


def test_every_recorded_bus_validates(buses_payload: list[dict[str, object]]) -> None:
    buses = [Bus.model_validate(b) for b in buses_payload]
    assert buses
    for bus in buses:
        assert bus.state_dtime.tzinfo == dt.UTC
        assert bus.current_station > 0


def test_recorded_buses_confirm_the_null_countdown_pattern(
    buses_payload: list[dict[str, object]],
) -> None:
    """Vehicles without a countdown are exactly those that have not departed."""
    buses = [Bus.model_validate(b) for b in buses_payload]
    for bus in buses:
        if bus.countdown is None:
            assert bus.last_station is None, f"vid {bus.vid} has no countdown but has departed"


def test_every_recorded_station_validates(stations_payload: list[dict[str, object]]) -> None:
    stations = [Station.model_validate(s) for s in stations_payload]
    assert len(stations) > 100
    assert all(p.gps_latitude and p.gps_longitude for s in stations for p in s.platforms)


def test_connection_detail_times(detail_payload: dict[str, object]) -> None:
    detail = ConnectionDetail.model_validate(detail_payload)
    assert detail.stops

    # Only the final stop carries an arrival time; the rest are departures.
    assert all(s.departureTime for s in detail.stops[:-1])
    assert detail.stops[-1].arrivalTime or detail.stops[-1].departureTime

    # Every stop resolves to a usable timetable time.
    assert all(isinstance(s.time, dt.time) for s in detail.stops)


def test_connection_detail_index_is_monotonic(detail_payload: dict[str, object]) -> None:
    """direction_id is derived from this, so it has to hold."""
    detail = ConnectionDetail.model_validate(detail_payload)
    idx = [s.index for s in detail.stops]
    assert idx == sorted(idx) or idx == sorted(idx, reverse=True)
