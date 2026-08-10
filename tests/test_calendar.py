import datetime as dt

import pytest

from dpmp_gtfs.static.calendar import (
    calendar_exceptions,
    czech_holidays,
    days_between,
    holidays_between,
    named_services,
    observed_holidays,
    service_from_codes,
    service_from_dates,
)
from dpmp_gtfs.types import Service

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


# --- dates -> service --------------------------------------------------------
#
# CIS publishes a day-by-day bitmap rather than a weekly pattern, because DPMP
# runs three different weekday timetables depending on whether schools are in
# session. Squeezing that back into calendar.txt plus calendar_dates.txt is
# only worth anything if it is exact, so these check the round trip rather
# than the shape of the answer.

YEAR = (dt.date(2026, 8, 10), dt.date(2027, 8, 10))
"""A full year from a Monday, the window a nightly rebuild produces."""


def _dates(predicate) -> frozenset[dt.date]:
    return frozenset(d for d in days_between(*YEAR) if predicate(d))


def _reconstruct(service: Service, start: dt.date, end: dt.date) -> frozenset[dt.date]:
    """The days a consumer reading calendar.txt and calendar_dates.txt would
    believe this service runs on."""
    days = {d for d in days_between(start, end) if d.weekday() in service.days}
    for row in calendar_exceptions([service], start, end):
        (days.add if row.added else days.discard)(row.date)
    return frozenset(days)


HOLIDAYS = observed_holidays(*YEAR)

PATTERNS = {
    "weekdays but not holidays": _dates(lambda d: d.weekday() < 5 and d not in HOLIDAYS),
    "weekends and holidays": _dates(lambda d: d.weekday() >= 5 or d in HOLIDAYS),
    "fridays only": _dates(lambda d: d.weekday() == 4),
    "school holidays only": _dates(
        lambda d: d.weekday() < 5 and d not in HOLIDAYS and d.month in (7, 8)
    ),
    "every third day": _dates(lambda d: (d - YEAR[0]).days % 3 == 0),
}


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_a_service_built_from_dates_runs_on_exactly_those_dates(name: str) -> None:
    dates = PATTERNS[name]
    service = service_from_dates(dates, *YEAR)

    for date in days_between(*YEAR):
        assert service.runs_on(date, holiday=date in HOLIDAYS) is (date in dates), date


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_the_written_calendar_reproduces_the_dates_it_came_from(name: str) -> None:
    """calendar.txt cannot say "weekdays in term time", so whatever the weekly
    pattern gets wrong has to come back out of calendar_dates.txt."""
    dates = PATTERNS[name]

    assert _reconstruct(service_from_dates(dates, *YEAR), *YEAR) == dates


def test_the_ordinary_patterns_need_no_exceptions_at_all() -> None:
    """A trip that really does run every weekday must not drag 250 rows of
    calendar_dates.txt along with it."""
    weekdays = service_from_dates(PATTERNS["weekdays but not holidays"], *YEAR)
    weekends = service_from_dates(PATTERNS["weekends and holidays"], *YEAR)

    assert weekdays.service_id == "wd"
    assert not weekdays.added and not weekdays.removed
    assert weekends.service_id == "sa-su+h"
    assert not weekends.added and not weekends.removed


def test_a_seasonal_service_says_it_is_described_by_dates() -> None:
    """Summer-only weekday trips are on the road too few days to give any
    weekday a majority, so the pattern is empty. The name says exactly that
    and nothing about weekdays: the days it does run are whatever part of the
    season is still inside the window, so a name drawn from them would change
    every night."""
    summer = service_from_dates(PATTERNS["school holidays only"], *YEAR)

    assert summer.days == frozenset()
    assert summer.base_id == "dates"
    assert summer.added == PATTERNS["school holidays only"]


# --- naming services --------------------------------------------------------


def test_two_variants_of_the_same_weekly_pattern_keep_separate_ids() -> None:
    """Term-time and school-holiday weekday trips share a weekly pattern and
    differ only in their days off. One id would give each the other's."""
    term = service_from_dates(
        PATTERNS["weekdays but not holidays"] - {dt.date(2026, 10, 29)}, *YEAR
    )
    other = service_from_dates(
        PATTERNS["weekdays but not holidays"] - {dt.date(2026, 10, 30)}, *YEAR
    )
    assert term.days == other.days == frozenset({0, 1, 2, 3, 4})

    named = named_services([term, other])

    assert named[term].service_id == "wd-20261029"
    assert named[other].service_id == "wd-20261030"


def test_variants_ending_on_the_same_day_are_still_told_apart() -> None:
    """The last day is usually enough, because it is a timetable changeover.
    Two variants ending on one still have to get two ids."""
    weekdays = PATTERNS["weekdays but not holidays"]
    last = dt.date(2026, 10, 30)

    one = service_from_dates(weekdays - {dt.date(2026, 10, 29), last}, *YEAR)
    two = service_from_dates(weekdays - {last}, *YEAR)

    named = named_services([one, two])
    ids = {named[one].service_id, named[two].service_id}

    assert len(ids) == 2
    assert ids == {"wd-20261030", "wd-20261030-2"}


def test_a_pattern_and_its_exception_only_twin_do_not_share_an_id() -> None:
    """Every weekday plus five holidays, against those five holidays and
    nothing else. Two different services; sharing an id would give the second
    one the first one's calendar row and put it on the road all year."""
    weekdays = PATTERNS["weekdays but not holidays"]
    extras = frozenset(sorted(HOLIDAYS)[:5])

    every_weekday_and_then_some = service_from_dates(weekdays | extras, *YEAR)
    only_the_extras = service_from_dates(extras, *YEAR)

    named = named_services([every_weekday_and_then_some, only_the_extras])

    assert named[every_weekday_and_then_some].service_id != named[only_the_extras].service_id


def test_the_ordinary_service_keeps_the_bare_name() -> None:
    """``wd`` is most of the network; it should not pick up a suffix because a
    variant of it turned up beside it."""
    weekdays = PATTERNS["weekdays but not holidays"]
    plain = service_from_dates(weekdays, *YEAR)
    with_a_day_off = service_from_dates(weekdays - {dt.date(2026, 10, 30)}, *YEAR)

    named = named_services([with_a_day_off, plain])

    assert named[plain].service_id == "wd"
    assert named[with_a_day_off].service_id == "wd-20261030"


# --- ids across nightly rebuilds ---------------------------------------------
#
# The window slides forward a day every night, so the elapsed days drop out of
# `added` and `removed`. Anything computed from all of those dates -- a hash, or
# a position in a list ordered by them -- renames services that have not
# changed; both cost a few hundred renamed trips a night on the real network,
# and the run below is what catches it.

BOUNDARY = dt.date(2026, 8, 31)
"""The end of a timetable period, of the kind that ends a seasonal service."""

NIGHTLY_RULES = {
    # Term time: every weekday except the school holidays running to the
    # boundary. Ends up as `wd` with those days removed.
    "term time": lambda d: d.weekday() < 5 and not (YEAR[0] <= d <= BOUNDARY),
    # The supplement that replaces it, and a second one that stops three days
    # earlier -- two variants that must not be confused for one another.
    "supplement": lambda d: d.weekday() < 5 and YEAR[0] <= d <= BOUNDARY,
    "short supplement": lambda d: d.weekday() < 5 and YEAR[0] <= d <= BOUNDARY - dt.timedelta(3),
}


def _ids_on(start: dt.date) -> dict[str, tuple[str, Service]]:
    """What a build starting on ``start`` would call each of the rules above."""
    end = start + dt.timedelta(days=365)
    services = {
        name: service_from_dates(
            frozenset(d for d in days_between(start, end) if rule(d)), start, end
        )
        for name, rule in NIGHTLY_RULES.items()
    }
    live = {name: s for name, s in services.items() if s.runs_at_all}
    naming = named_services(set(live.values()))
    return {name: (naming[s].service_id, s) for name, s in live.items()}


def test_nightly_rebuilds_do_not_rename_services_across_a_period_boundary() -> None:
    """A fortnight of builds walking over 31 August. A service may be renamed
    when what it does changes -- a supplement that has run out really is a
    different service -- but never merely because the window moved or because
    some *other* service disappeared from beside it."""
    previous: dict[str, tuple[str, Service]] | None = None

    for offset in range(15):
        start = BOUNDARY - dt.timedelta(days=8) + dt.timedelta(days=offset)
        current = _ids_on(start)

        if previous is not None:
            for name in previous.keys() & current.keys():
                was, before = previous[name]
                now, after = current[name]
                if before.base_id == after.base_id and _last_exception(before) == _last_exception(
                    after
                ):
                    assert was == now, f"{name} renamed {was} -> {now} on {start}"
        previous = current


def test_the_two_supplements_never_share_an_id_on_any_night() -> None:
    """They end three days apart, which is the whole reason the id is built on
    the last day: it is the one thing about them the window cannot erode."""
    for offset in range(15):
        start = BOUNDARY - dt.timedelta(days=8) + dt.timedelta(days=offset)
        ids = {name: pair[0] for name, pair in _ids_on(start).items()}

        assert len(set(ids.values())) == len(ids), f"{ids} collide on {start}"


def _last_exception(service: Service) -> dt.date | None:
    exceptions = service.added | service.removed
    return max(exceptions) if exceptions else None


def test_an_exception_outranks_the_holiday_rule() -> None:
    """A trip CIS says runs on Labour Day runs on Labour Day, whatever the
    network does around it."""
    labour_day = dt.date(2026, 5, 1)  # Friday
    weekdays_including_the_holiday = Service(
        days=frozenset({0, 1, 2, 3, 4}), holidays=False, added=frozenset({labour_day})
    )

    assert weekdays_including_the_holiday.runs_on(labour_day, holiday=True) is True


def test_a_trip_that_runs_on_no_day_of_the_window_has_no_id() -> None:
    """CIS keeps trips whose bitmap has run out. The builder drops them, and
    this is how it finds out."""
    nothing = service_from_dates(frozenset(), *YEAR)

    assert nothing.runs_at_all is False
    with pytest.raises(ValueError):
        _ = nothing.service_id
