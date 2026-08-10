"""Tests for reading days of operation out of the NeTEx archives.

The fixtures are slices of the real archive published on 2026-08-07, cut down
to a few journeys each but with their ``ValidDayBits`` and passing times
untouched:

* line 655001 (trolleybus 1) in the version in force from 2026-07-01 and the
  superseded one from 2026-01-01, which compete for the same dates;
* line 655002 (trolleybus 2) in the school-holiday supplement that expires on
  2026-08-31 and the year-round version that takes over from it, which *tile*
  the timeline instead. Its trip 9 leaves at 04:32 in both; its trip 21 leaves
  at 05:15 in one and 05:20 in the other, so the number means a different
  journey either side of the changeover.

Everything asserted below was checked against the whole archive first, so a
fixture that drifts from reality fails here rather than quietly agreeing with
itself.
"""

import datetime as dt
import logging
import zipfile
from pathlib import Path

import pytest

from dpmp_gtfs.cis.calendars import build_calendars

FIXTURES = Path(__file__).parent / "fixtures" / "netex"
CURRENT = FIXTURES / "line-655001-2026-07-01.xml"
SUPERSEDED = FIXTURES / "line-655001-2026-01-01.xml"
SUMMER = FIXTURES / "line-655002-2026-07-27.xml"
YEAR_ROUND = FIXTURES / "line-655002-2026-01-01.xml"

# A fortnight starting on a Monday, so a weekday pattern and a weekend one are
# each visible twice over and cannot be confused with an off-by-one.
START = dt.date(2026, 8, 10)
END = dt.date(2026, 8, 23)

WEEKDAYS = frozenset({dt.date(2026, 8, day) for day in (10, 11, 12, 13, 14, 17, 18, 19, 20, 21)})
WEEKEND = frozenset({dt.date(2026, 8, day) for day in (15, 16, 22, 23)})


def archive(tmp_path: Path, *sources: Path, name: str = "netex.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for source in sources:
            zf.write(source, source.name)
    return path


def test_each_trip_gets_the_days_its_day_type_marks(tmp_path: Path) -> None:
    """Trip 46 (06:36 from Slovany,točna) is the one the API calls a weekend
    trip; CIS, DPMP's own timetable and the paper stop sign all say Mon-Fri."""
    calendars = build_calendars([archive(tmp_path, CURRENT)], START, END)

    assert calendars[("655001", 46)] == WEEKDAYS
    assert calendars[("655001", 3)] == WEEKEND
    assert calendars[("655001", 382)] == WEEKDAYS | WEEKEND


def test_a_state_holiday_runs_the_weekend_service(tmp_path: Path) -> None:
    """28 September 2026 is a Monday and Den české státnosti. The bitmap is
    day-by-day, so this needs no holiday calendar of ours to come out right."""
    holiday = dt.date(2026, 9, 28)
    calendars = build_calendars([archive(tmp_path, CURRENT)], holiday, holiday)

    assert calendars[("655001", 3)] == frozenset({holiday})
    assert calendars[("655001", 46)] == frozenset()


def test_days_outside_the_window_are_dropped(tmp_path: Path) -> None:
    """The bitmap runs to 2030; a feed valid for a year must not carry the
    other four."""
    week = build_calendars([archive(tmp_path, CURRENT)], START, START + dt.timedelta(days=4))

    assert week[("655001", 46)] == frozenset(
        {START + dt.timedelta(days=offset) for offset in range(5)}
    )


def test_the_version_in_force_replaces_the_superseded_one(tmp_path: Path) -> None:
    """Both versions of line 655001 are valid on the build date and both
    describe trip 60 -- as a weekday trip in January's version and a weekend
    one in July's. Unioning them would put it on the road all week."""
    old = build_calendars([archive(tmp_path, SUPERSEDED, name="old.zip")], START, END)
    assert old[("655001", 60)] == WEEKDAYS

    both = build_calendars([archive(tmp_path, CURRENT, SUPERSEDED)], START, END)
    assert both[("655001", 60)] == WEEKEND


def test_a_version_that_has_not_started_yet_is_ignored(tmp_path: Path) -> None:
    """July's version does not exist as far as a build in March is concerned,
    so January's is the one still standing."""
    march = dt.date(2026, 3, 2)
    calendars = build_calendars(
        [archive(tmp_path, CURRENT, SUPERSEDED)], march, march + dt.timedelta(days=4)
    )

    assert ("655001", 46) not in calendars, "46 exists only in July's version"
    assert calendars[("655001", 60)] == frozenset(
        {march + dt.timedelta(days=offset) for offset in range(5)}
    )


def test_another_operators_lines_are_not_read(tmp_path: Path) -> None:
    """Both national archives carry every carrier in the country; only DPMP's
    own IČ may contribute anything."""
    someone_else = tmp_path / "other.xml"
    someone_else.write_bytes(CURRENT.read_bytes().replace(b"63217066", b"12345678"))

    assert build_calendars([archive(tmp_path, someone_else)], START, END) == {}


# --- versions that tile rather than compete ----------------------------------
#
# A line ships a year-round version alongside a supplement covering the school
# holidays. Both are valid on a build date in August, but the supplement runs
# out on the 31st and the year-round one takes over. Letting the supplement win
# the whole window -- it has the later FromDate -- took lines 2 and 6 dark from
# 1 September to the end of a feed claiming to be valid for a year.

SEPTEMBER = dt.date(2026, 9, 30)


def test_a_trip_keeps_running_when_the_next_version_still_means_it(tmp_path: Path) -> None:
    """Trip 9 leaves at 04:32 in both versions, so it is the same journey and
    the year-round version's days after 31 August are its days."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    days = calendars[("655002", 9)]
    assert max(days) == SEPTEMBER
    assert dt.date(2026, 9, 1) in days


def test_a_renumbered_trip_stops_rather_than_borrowing_another_journeys_days(
    tmp_path: Path,
) -> None:
    """Trip 21 leaves at 05:15 before the changeover and 05:20 after it, so the
    number means a different journey in September. This feed publishes the
    05:15 one; giving it September's days would send a passenger to a stop five
    minutes after the only bus that calls there."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    days = calendars[("655002", 21)]
    assert days, "the trip still runs while its own version is in force"
    assert max(days) < dt.date(2026, 9, 1)


def test_only_the_trips_of_the_version_in_force_are_reported(tmp_path: Path) -> None:
    """The API serves the timetable in force today, so its trip numbers are
    that version's. A number only a later version has belongs to a journey this
    feed does not carry."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)
    only_later = build_calendars(
        [archive(tmp_path, YEAR_ROUND, name="later.zip")], START, SEPTEMBER
    )

    assert set(calendars) == {("655002", 9), ("655002", 21)}
    assert set(only_later) == set(calendars), "with nothing in force, the later one is"


def test_a_line_whose_timetable_runs_out_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The feed is then honestly incomplete rather than wrong, but it is
    incomplete quietly, and an operator watching a line go dark deserves to be
    told which and when."""
    with caplog.at_level(logging.WARNING):
        build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    assert "line 655002: 1 of 2 trips have no CIS days after" in caplog.text
