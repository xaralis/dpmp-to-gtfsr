"""Tests for reading days of operation out of the NeTEx archives.

The fixtures are slices of the real archive published on 2026-08-07, cut down
to a few journeys each but with their ``ValidDayBits`` and passing times
untouched:

* line 655001 (trolleybus 1) in the version in force from 2026-07-01 and the
  superseded one from 2026-01-01, which compete for the same dates;
* line 655009 (bus 9) in the school-holiday supplement that expires on
  2026-08-31 and the year-round version that takes over from it, which *tile*
  the timeline instead. Its three journeys cover all three ways a trip number
  can behave across the changeover: trip 1 is the same journey in both
  versions, trip 31 leaves three minutes later in the second, and trip 23
  leaves at the same minute and then diverges from the eighth stop onwards.

Everything asserted below was checked against the whole archive first, so a
fixture that drifts from reality fails here rather than quietly agreeing with
itself.
"""

import datetime as dt
import logging
import re
import zipfile
from pathlib import Path

import pytest

from dpmp_gtfs.cis.calendars import build_calendars

FIXTURES = Path(__file__).parent / "fixtures" / "netex"
CURRENT = FIXTURES / "line-655001-2026-07-01.xml"
SUPERSEDED = FIXTURES / "line-655001-2026-01-01.xml"
SUMMER = FIXTURES / "line-655009-2026-07-22.xml"
YEAR_ROUND = FIXTURES / "line-655009-2026-07-01.xml"

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

    assert calendars[("655001", 46)].days == WEEKDAYS
    assert calendars[("655001", 3)].days == WEEKEND
    assert calendars[("655001", 382)].days == WEEKDAYS | WEEKEND


def test_a_state_holiday_runs_the_weekend_service(tmp_path: Path) -> None:
    """28 September 2026 is a Monday and Den české státnosti. The bitmap is
    day-by-day, so this needs no holiday calendar of ours to come out right."""
    holiday = dt.date(2026, 9, 28)
    calendars = build_calendars([archive(tmp_path, CURRENT)], holiday, holiday)

    assert calendars[("655001", 3)].days == frozenset({holiday})
    assert calendars[("655001", 46)].days == frozenset()


def test_days_outside_the_window_are_dropped(tmp_path: Path) -> None:
    """The bitmap runs to 2030; a feed valid for a year must not carry the
    other four."""
    week = build_calendars([archive(tmp_path, CURRENT)], START, START + dt.timedelta(days=4))

    assert week[("655001", 46)].days == frozenset(
        {START + dt.timedelta(days=offset) for offset in range(5)}
    )


def test_the_version_in_force_replaces_the_superseded_one(tmp_path: Path) -> None:
    """Both versions of line 655001 are valid on the build date and both
    describe trip 60 -- as a weekday trip in January's version and a weekend
    one in July's. Unioning them would put it on the road all week."""
    old = build_calendars([archive(tmp_path, SUPERSEDED, name="old.zip")], START, END)
    assert old[("655001", 60)].days == WEEKDAYS

    both = build_calendars([archive(tmp_path, CURRENT, SUPERSEDED)], START, END)
    assert both[("655001", 60)].days == WEEKEND


def test_a_version_that_has_not_started_yet_is_ignored(tmp_path: Path) -> None:
    """July's version does not exist as far as a build in March is concerned,
    so January's is the one still standing."""
    march = dt.date(2026, 3, 2)
    calendars = build_calendars(
        [archive(tmp_path, CURRENT, SUPERSEDED)], march, march + dt.timedelta(days=4)
    )

    assert ("655001", 46) not in calendars, "46 exists only in July's version"
    assert calendars[("655001", 60)].days == frozenset(
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
CHANGEOVER = dt.date(2026, 9, 1)


def test_a_trip_keeps_running_when_the_next_version_still_means_it(tmp_path: Path) -> None:
    """Trip 1 calls at the same minute at every stop in both versions, so it is
    the same journey and the year-round version's days after 31 August are
    its."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    days = calendars[("655009", 1)].days
    assert max(days) == SEPTEMBER
    assert CHANGEOVER in days


def test_a_trip_renumbered_at_its_first_stop_stops_at_the_changeover(tmp_path: Path) -> None:
    """Trip 31 leaves at 07:50 before the changeover and 07:53 after it. Giving
    it September's days would publish a departure three minutes before the only
    bus that calls there."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    days = calendars[("655009", 31)].days
    assert days, "the trip still runs while its own version is in force"
    assert max(days) < CHANGEOVER


def test_a_trip_that_diverges_only_later_in_its_run_also_stops(tmp_path: Path) -> None:
    """Trip 23 leaves at 07:08 either side of the changeover and is a minute
    apart from the eighth stop onwards. Comparing only the first departure
    would call it the same journey and hand it September's days -- so this is
    the test that says the comparison has to be every call time."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    days = calendars[("655009", 23)].days
    assert days
    assert max(days) < CHANGEOVER


def test_only_the_trips_of_the_version_in_force_are_reported(tmp_path: Path) -> None:
    """The API serves the timetable in force today, so its trip numbers are
    that version's. A number only a later version has belongs to a journey this
    feed does not carry."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    assert set(calendars) == {("655009", 1), ("655009", 23), ("655009", 31)}


def test_a_line_whose_timetable_runs_out_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The feed is then honestly incomplete rather than wrong, but it is
    incomplete quietly, and an operator watching a line go dark deserves to be
    told which and when."""
    with caplog.at_level(logging.WARNING):
        build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    assert "line 655009: 2 of 3 trips have no CIS days after" in caplog.text


def test_a_journey_with_no_times_at_all_is_not_extended(tmp_path: Path) -> None:
    """Nothing then shows it is the same journey as the one in force, and the
    burden of proof sits on extending a trip rather than on stopping it. A file
    whose passing times went missing must not quietly restore a blind union."""
    stripped = []
    for source in (SUMMER, YEAR_ROUND):
        body = re.sub(
            r"\s*<passingTimes.*?</passingTimes>",
            "",
            source.read_text(encoding="utf8"),
            flags=re.DOTALL,
        )
        assert body.count("<ServiceJourney ") == 3, "the journeys themselves must survive"
        target = tmp_path / f"timeless-{source.name}"
        target.write_text(body, encoding="utf8")
        stripped.append(target)

    calendars = build_calendars([archive(tmp_path, *stripped)], START, SEPTEMBER)

    assert max(calendars[("655009", 1)].days) < CHANGEOVER


def test_the_origin_does_not_move_when_the_window_does(tmp_path: Path) -> None:
    """A trip's days shrink from the front every night as the feed's window
    slides. What the days were read from does not, and that is the only thing a
    service id can safely be built out of."""
    zipped = archive(tmp_path, SUMMER, YEAR_ROUND)

    today = build_calendars([zipped], START, SEPTEMBER)
    tomorrow = build_calendars(
        [zipped], START + dt.timedelta(days=1), SEPTEMBER + dt.timedelta(days=1)
    )

    key = ("655009", 1)
    assert today[key].days != tomorrow[key].days, "the days did move"
    assert today[key].origin == tomorrow[key].origin
    assert today[key].origin, "and it says something"


def test_trips_on_different_calendars_get_different_origins(tmp_path: Path) -> None:
    """Otherwise two services that share a weekly pattern would share an id and
    one of them would inherit the other's days."""
    calendars = build_calendars([archive(tmp_path, SUMMER, YEAR_ROUND)], START, SEPTEMBER)

    assert calendars[("655009", 1)].origin != calendars[("655009", 23)].origin
