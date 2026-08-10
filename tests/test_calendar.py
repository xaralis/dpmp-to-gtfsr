import datetime as dt

import pytest

from dpmp_gtfs.static.calendar import (
    calendar_exceptions,
    czech_holidays,
    holidays_between,
    service_from_codes,
)

# --- code -> service --------------------------------------------------------


def test_weekday_trips() -> None:
    service = service_from_codes(["X", "@"])
    assert service.days == frozenset({0, 1, 2, 3, 4})
    assert service.service_id == "wd"


def test_weekend_trips() -> None:
    service = service_from_codes(["6", "+", "@"])
    assert service.days == frozenset({5, 6})
    assert service.service_id == "sa-su+h"


def test_low_floor_is_not_a_calendar_code() -> None:
    # "@" alone would leave a service running on no days at all.
    with pytest.raises(ValueError):
        _ = service_from_codes(["@"]).service_id


def test_upper_and_lower_x_are_different_codes() -> None:
    # "x" is a stop-level "on request" marker and must never mean weekdays.
    assert service_from_codes(["X"]).days
    assert not service_from_codes(["x"]).days


def test_weekday_flags_match_gtfs_column_order() -> None:
    assert service_from_codes(["X", "@"]).weekday_flags == (1, 1, 1, 1, 1, 0, 0)
    assert service_from_codes(["@", "6"]).weekday_flags == (0, 0, 0, 0, 0, 1, 0)
    assert service_from_codes(["@", "+"]).weekday_flags == (0, 0, 0, 0, 0, 0, 1)
    assert service_from_codes(["X", "@", "6", "+"]).weekday_flags == (1, 1, 1, 1, 1, 1, 1)


# --- per-weekday codes ------------------------------------------------------
#
# The airport shuttle (line 90, Hlavní nádraží <-> Letiště terminál) runs on
# the days flights leave, so its trips carry JDF's single-weekday codes 1..7
# instead of X/6/+. Before these were understood, one such trip took the whole
# feed build down with "service runs on no days at all".


def test_a_trip_running_only_on_monday_and_friday() -> None:
    service = service_from_codes(["1", "5", "@"])
    assert service.days == frozenset({0, 4})
    assert service.weekday_flags == (1, 0, 0, 0, 1, 0, 0)
    assert service.service_id == "mo-fr"


def test_a_single_midweek_day() -> None:
    service = service_from_codes(["3", "@"])
    assert service.weekday_flags == (0, 0, 1, 0, 0, 0, 0)
    assert service.service_id == "we"


def test_plain_sunday_is_not_the_same_service_as_sunday_with_holidays() -> None:
    """JDF ``7`` is "jede v neděli"; ``+`` is "jede v neděli a ve státem
    uznané svátky". Same weekday column, different behaviour on a holiday --
    so they must not collapse onto one service_id."""
    plain = service_from_codes(["7", "@"])
    with_holidays = service_from_codes(["+", "@"])

    assert plain.weekday_flags == with_holidays.weekday_flags == (0, 0, 0, 0, 0, 0, 1)
    assert plain.service_id != with_holidays.service_id
    assert (plain.service_id, with_holidays.service_id) == ("su", "su+h")

    # 2026-12-25 is a Friday and a state holiday.
    christmas = dt.date(2026, 12, 25)
    assert christmas.weekday() == 4
    assert plain.runs_on(christmas, holiday=True) is False
    assert with_holidays.runs_on(christmas, holiday=True) is True


def test_a_full_working_week_still_collapses_to_wd() -> None:
    """Spelled out day by day, it is the same service as ``X`` and must not
    become a second calendar row saying the same thing."""
    assert service_from_codes(["1", "2", "3", "4", "5"]).service_id == "wd"


def test_a_weekday_code_beyond_the_working_week_keeps_its_own_name() -> None:
    assert service_from_codes(["X", "6"]).service_id == "wd-sa"


# --- movable feasts ---------------------------------------------------------

# Easter is the only part of the calendar that moves, and getting it wrong
# silently misplaces two holidays a year. These dates are the reference the
# hand-rolled implementation was checked against before it was replaced by the
# holidays library.
EASTER_MONDAY = {
    2024: dt.date(2024, 4, 1),
    2025: dt.date(2025, 4, 21),
    2026: dt.date(2026, 4, 6),
    2027: dt.date(2027, 3, 29),
    2030: dt.date(2030, 4, 22),
    2038: dt.date(2038, 4, 26),
}


@pytest.mark.parametrize(("year", "monday"), sorted(EASTER_MONDAY.items()))
def test_easter_monday_and_good_friday_are_present(year: int, monday: dt.date) -> None:
    found = czech_holidays(year)
    assert monday in found, "Velikonoční pondělí"
    assert monday - dt.timedelta(days=3) in found, "Velký pátek"


def test_every_year_has_both_movable_feasts() -> None:
    """A library upgrade that dropped them would otherwise pass unnoticed."""
    for year in range(2024, 2040):
        movable = [d for d in czech_holidays(year) if (d.month, d.day) not in FIXED]
        assert len(movable) == 2, f"{year} has {len(movable)} movable holidays"


FIXED = {
    (1, 1),
    (5, 1),
    (5, 8),
    (7, 5),
    (7, 6),
    (9, 28),
    (10, 28),
    (11, 17),
    (12, 24),
    (12, 25),
    (12, 26),
}


# --- holidays ---------------------------------------------------------------


def test_holiday_set_for_a_known_year() -> None:
    holidays = czech_holidays(2026)
    assert len(holidays) == 13
    assert dt.date(2026, 4, 3) in holidays  # Velký pátek
    assert dt.date(2026, 4, 6) in holidays  # Velikonoční pondělí
    assert dt.date(2026, 7, 5) in holidays  # Cyril a Metoděj
    assert dt.date(2026, 12, 26) in holidays
    assert dt.date(2026, 3, 8) not in holidays  # MDŽ is not a public holiday


def test_holidays_between_spans_years() -> None:
    found = holidays_between(dt.date(2026, 12, 20), dt.date(2027, 1, 5))
    assert found == {
        dt.date(2026, 12, 24),
        dt.date(2026, 12, 25),
        dt.date(2026, 12, 26),
        dt.date(2027, 1, 1),
    }


# --- runs_on ----------------------------------------------------------------


def test_holiday_takes_the_sunday_timetable() -> None:
    weekday = service_from_codes(["X", "@"])
    sunday_only = service_from_codes(["@", "+"])
    # 2026-05-01 (Svátek práce) is a Friday.
    labour_day = dt.date(2026, 5, 1)
    assert labour_day.weekday() == 4

    assert weekday.runs_on(labour_day, holiday=False) is True
    assert weekday.runs_on(labour_day, holiday=True) is False
    assert sunday_only.runs_on(labour_day, holiday=False) is False
    assert sunday_only.runs_on(labour_day, holiday=True) is True


# --- calendar_dates ---------------------------------------------------------


def test_holiday_on_a_weekday_removes_and_adds_the_right_services() -> None:
    services = [service_from_codes(["X", "@"]), service_from_codes(["@", "+"])]
    labour_day = dt.date(2026, 5, 1)  # Friday

    got = {(e.service_id, e.added) for e in calendar_exceptions(services, labour_day, labour_day)}
    assert got == {("wd", False), ("su+h", True)}


def test_a_holiday_falling_on_a_sunday_needs_no_exceptions() -> None:
    """The regular calendar already runs the Sunday service that day."""
    services = [service_from_codes(["X", "@"]), service_from_codes(["@", "+"])]
    # 2027-03-28 is Easter Sunday.
    easter = dt.date(2027, 3, 28)
    assert easter.weekday() == 6
    assert list(calendar_exceptions(services, easter, easter)) == []


def test_a_daily_service_is_untouched_by_holidays() -> None:
    """Night lines 98 and 99 run every day, so no holiday changes anything for
    them -- and emitting spurious exceptions would be a validator warning."""
    daily = service_from_codes(["X", "@", "6", "+"])
    labour_day = dt.date(2026, 5, 1)
    assert list(calendar_exceptions([daily], labour_day, labour_day)) == []


def test_saturday_holiday_swaps_saturday_for_sunday() -> None:
    services = [service_from_codes(["@", "6"]), service_from_codes(["@", "+"])]
    # 2026-08-08 is not a holiday; use 2026-12-26 (Saturday, 2. svátek vánoční).
    boxing_day = dt.date(2026, 12, 26)
    assert boxing_day.weekday() == 5

    got = {(e.service_id, e.added) for e in calendar_exceptions(services, boxing_day, boxing_day)}
    assert got == {("sa", False), ("su+h", True)}
