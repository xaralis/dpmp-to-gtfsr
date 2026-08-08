"""Tests for service-day arithmetic.

GTFS counts from the start of a service day, not from midnight, and the two
diverge for exactly the trips that are hardest to get right: the handful that
run past midnight. Both feeds have to agree on which service day a running
vehicle belongs to, or consumers matching a TripUpdate to its trip find
nothing.
"""

import datetime as dt

import pytest

from dpmp_gtfs.timeutil import (
    PRAGUE,
    format_clock,
    format_gtfs_time,
    service_day_date,
    service_day_seconds,
)

# The three trips in the network that cross midnight, as the static builder
# writes them: line 3 trips 322/324 (23:55 -> 00:23) and line 98 trip 9
# (23:58 -> 00:52). Their *first departure* is before 24:00, which is what
# makes them the interesting case.
BEFORE_MIDNIGHT = 23 * 3600 + 58 * 60
PAST_MIDNIGHT = 24 * 3600 + 10 * 60


# --- formatting -------------------------------------------------------------


def test_hours_past_24_are_kept() -> None:
    """Wrapping these to 00:23 would turn a trip into one that runs backwards."""
    assert format_gtfs_time(24 * 3600 + 23 * 60) == "24:23:00"
    assert format_clock(24 * 3600 + 23 * 60) == "24:23"


def test_ordinary_times_are_zero_padded() -> None:
    assert format_gtfs_time(4 * 3600 + 25 * 60) == "04:25:00"
    assert format_clock(4 * 3600 + 25 * 60) == "04:25"


# --- service_day_seconds ----------------------------------------------------


def test_a_daytime_observation_is_taken_at_face_value() -> None:
    moment = dt.datetime(2026, 8, 9, 10, 0, tzinfo=PRAGUE)
    assert service_day_seconds(moment, 10 * 3600) == 10 * 3600


def test_an_observation_after_midnight_belongs_to_the_previous_service_day() -> None:
    """00:10 on a trip scheduled for 24:10 is 24:10, not 00:10."""
    moment = dt.datetime(2026, 8, 9, 0, 10, tzinfo=PRAGUE)
    assert service_day_seconds(moment, PAST_MIDNIGHT) == PAST_MIDNIGHT


# --- service_day_date -------------------------------------------------------


def test_a_trip_departing_before_midnight_keeps_its_service_date_after_it() -> None:
    """Regression: the service date used to be derived from the schedule alone.

    Line 98 trip 9 departs at 23:58, so its first departure is below 24:00 and
    the old test (``first_departure // DAY``) yielded zero -- reporting a
    vehicle still running at 00:30 as belonging to the *new* calendar day. It
    belongs to the day the trip started, and consumers key on that.
    """
    moment = dt.datetime(2026, 8, 9, 0, 30, tzinfo=PRAGUE)
    assert service_day_date(moment, BEFORE_MIDNIGHT) == dt.date(2026, 8, 8)


def test_a_trip_scheduled_past_24_also_keeps_the_starting_date() -> None:
    moment = dt.datetime(2026, 8, 9, 0, 10, tzinfo=PRAGUE)
    assert service_day_date(moment, PAST_MIDNIGHT) == dt.date(2026, 8, 8)


def test_before_midnight_the_service_date_is_simply_today() -> None:
    moment = dt.datetime(2026, 8, 8, 23, 59, tzinfo=PRAGUE)
    assert service_day_date(moment, BEFORE_MIDNIGHT) == dt.date(2026, 8, 8)


@pytest.mark.parametrize("hour", [6, 9, 12, 15, 18, 21])
def test_ordinary_trips_are_never_shifted(hour: int) -> None:
    """The midnight fix must not disturb the 2,725 trips that do not cross it."""
    moment = dt.datetime(2026, 8, 8, hour, 30, tzinfo=PRAGUE)
    assert service_day_date(moment, hour * 3600) == dt.date(2026, 8, 8)


def test_utc_input_is_converted_before_the_day_is_decided() -> None:
    """``state_dtime`` arrives in UTC; in summer Prague is two hours ahead, so
    22:30 UTC is already the next day locally."""
    moment = dt.datetime(2026, 8, 8, 22, 30, tzinfo=dt.UTC)
    assert service_day_date(moment, 30 * 60) == dt.date(2026, 8, 9)
