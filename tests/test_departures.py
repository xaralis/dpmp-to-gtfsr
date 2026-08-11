"""Tests for the departure board.

The realtime join is the easy half. The hard half is the calendar: a board is
drawn from the timetable rather than from vehicles, so without it a Tuesday
evening would offer Sunday trips.
"""

import datetime as dt

from dpmp_gtfs.realtime.departures import build_departures
from dpmp_gtfs.realtime.index import (
    ScheduledStop,
    ScheduledTrip,
    ServiceCalendar,
    StaticIndex,
)
from dpmp_gtfs.timeutil import PRAGUE

WEEKDAY = dt.datetime(2026, 8, 11, 10, 0, tzinfo=PRAGUE)  # a Tuesday
SUNDAY = dt.datetime(2026, 8, 9, 10, 0, tzinfo=PRAGUE)

CALENDAR = ServiceCalendar(
    weekdays={
        "wd": (True,) * 5 + (False, False),
        "su": (False,) * 6 + (True,),
    },
    window={
        "wd": (dt.date(2026, 1, 1), dt.date(2027, 1, 1)),
        "su": (dt.date(2026, 1, 1), dt.date(2027, 1, 1)),
    },
)


def _trip(tid: str, service: str, seconds: int, line: str = "3") -> ScheduledTrip:
    return ScheduledTrip(
        trip_id=tid,
        route_id="L3",
        stops=(
            ScheduledStop("S1P1", 1, 0, seconds, "U Marka", (50.03, 15.77)),
            ScheduledStop("S2P1", 2, 1, seconds + 300, "Hlavní nádraží", (50.04, 15.76)),
        ),
        service_id=service,
        headsign="Hlavní nádraží",
        line=line,
    )


def _index(*trips: ScheduledTrip) -> StaticIndex:
    return StaticIndex({t.trip_id: t for t in trips}, CALENDAR)


def test_a_weekday_board_excludes_the_sunday_service() -> None:
    """The whole reason the index had to learn about calendars."""
    index = _index(
        _trip("L3C1", "wd", 11 * 3600),
        _trip("L3C2", "su", 11 * 3600 + 60),
    )
    board = build_departures("S1P1", index, [], WEEKDAY)
    assert [d.trip_id for d in board] == ["L3C1"]


def test_a_sunday_board_excludes_the_working_day_service() -> None:
    index = _index(
        _trip("L3C1", "wd", 11 * 3600),
        _trip("L3C2", "su", 11 * 3600 + 60),
    )
    board = build_departures("S1P1", index, [], SUNDAY)
    assert [d.trip_id for d in board] == ["L3C2"]


def test_departures_already_gone_are_not_shown() -> None:
    index = _index(_trip("L3C1", "wd", 9 * 3600), _trip("L3C2", "wd", 11 * 3600))
    board = build_departures("S1P1", index, [], WEEKDAY)
    assert [d.trip_id for d in board] == ["L3C2"]


def test_the_board_is_ordered_by_departure() -> None:
    index = _index(
        _trip("L3C3", "wd", 13 * 3600),
        _trip("L3C1", "wd", 11 * 3600),
        _trip("L3C2", "wd", 12 * 3600),
    )
    board = build_departures("S1P1", index, [], WEEKDAY)
    assert [d.scheduled for d in board] == ["11:00", "12:00", "13:00"]


def test_the_last_stop_of_a_trip_is_not_a_departure() -> None:
    """Nobody boards a trip that terminates there."""
    index = _index(_trip("L3C1", "wd", 11 * 3600))
    assert build_departures("S2P1", index, [], WEEKDAY) == []


def test_a_trip_past_midnight_still_belongs_to_the_previous_service_day() -> None:
    """Night lines are written 24:xx and depart after midnight. A board built
    only from today's service day would lose them exactly when they are the
    only thing running."""
    index = _index(_trip("L99C1", "wd", 24 * 3600 + 30 * 60, line="99"))
    just_after_midnight = dt.datetime(2026, 8, 12, 0, 10, tzinfo=PRAGUE)  # Wednesday

    board = build_departures("S1P1", index, [], just_after_midnight)
    assert [d.scheduled for d in board] == ["24:30"]
    assert board[0].in_seconds == 20 * 60


def test_a_delay_shifts_the_expected_time_but_not_the_timetable() -> None:
    from dpmp_gtfs.realtime.view import VehicleView

    index = _index(_trip("L3C1", "wd", 11 * 3600))
    running = VehicleView(
        vehicle_id="1",
        line="3",
        trolleybus=False,
        trip_id="L3C1",
        route_id="L3",
        destination="x",
        latitude=50.0,
        longitude=15.0,
        reported_at="",
        delay_seconds=120,
        previous_stop=None,
        next_stop=None,
        heading=None,
        headsign="Hlavní nádraží",
        stops_total=2,
        stop_index=0,
    )

    board = build_departures("S1P1", index, [running], WEEKDAY)
    assert board[0].scheduled == "11:00"
    assert board[0].expected == "11:02"
    assert board[0].delay_seconds == 120


def test_a_trip_with_no_vehicle_claims_no_delay() -> None:
    """Not the same as running on time -- most of the board, most of the time."""
    index = _index(_trip("L3C1", "wd", 11 * 3600))
    board = build_departures("S1P1", index, [], WEEKDAY)
    assert board[0].delay_seconds is None
    assert board[0].expected == board[0].scheduled


def test_an_unknown_stop_yields_an_empty_board() -> None:
    assert build_departures("S99P9", _index(_trip("L3C1", "wd", 11 * 3600)), [], WEEKDAY) == []


# --- the calendar itself -----------------------------------------------------


def test_an_exception_overrides_the_weekday_flags() -> None:
    """This is how holidays work: the network keeps its Sunday timetable, which
    the weekday flags alone cannot say."""
    calendar = ServiceCalendar(
        weekdays={"wd": (True,) * 5 + (False, False)},
        window={"wd": (dt.date(2026, 1, 1), dt.date(2027, 1, 1))},
        exceptions={("wd", dt.date(2026, 5, 1)): False},
    )
    assert calendar.runs_on("wd", dt.date(2026, 4, 30)) is True
    assert calendar.runs_on("wd", dt.date(2026, 5, 1)) is False


def test_a_service_outside_its_validity_window_does_not_run() -> None:
    calendar = ServiceCalendar(
        weekdays={"wd": (True,) * 7},
        window={"wd": (dt.date(2026, 1, 1), dt.date(2026, 6, 1))},
    )
    assert calendar.runs_on("wd", dt.date(2026, 5, 31)) is True
    assert calendar.runs_on("wd", dt.date(2026, 6, 2)) is False


def test_a_feed_without_calendar_data_still_shows_a_board() -> None:
    """Degrading to "show everything" beats degrading to "show nothing"."""
    assert ServiceCalendar().runs_on("anything", dt.date(2026, 8, 11)) is True


# --- station boards ----------------------------------------------------------


def test_a_station_board_merges_its_platforms() -> None:
    """Until a line is picked, stations are the only thing on the map to click,
    so a board that answered only for platforms answered for nothing."""
    there = _trip("L3C1", "wd", 11 * 3600)
    back = ScheduledTrip(
        trip_id="L3C2",
        route_id="L3",
        stops=(
            ScheduledStop("S1P2", 1, 0, 11 * 3600 + 120, "U Marka", (50.03, 15.77)),
            ScheduledStop("S9P1", 9, 1, 11 * 3600 + 400, "Jinam", (50.05, 15.75)),
        ),
        service_id="wd",
        headsign="Jinam",
        line="3",
    )
    index = _index(there, back)

    assert [d.trip_id for d in build_departures("S1P1", index, [], WEEKDAY)] == ["L3C1"]
    assert [d.trip_id for d in build_departures("S1P2", index, [], WEEKDAY)] == ["L3C2"]
    assert [d.trip_id for d in build_departures("S1", index, [], WEEKDAY)] == ["L3C1", "L3C2"]


def test_a_station_board_says_which_platform_each_departure_leaves_from() -> None:
    index = _index(_trip("L3C1", "wd", 11 * 3600))
    board = build_departures("S1", index, [], WEEKDAY)
    assert board[0].stop_id == "S1P1"
    assert board[0].platform == "1"
