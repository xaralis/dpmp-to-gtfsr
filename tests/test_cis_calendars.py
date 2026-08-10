"""Tests for reading days of operation out of the NeTEx archives.

The fixtures are slices of the real archive published on 2026-08-07: line
655001 (trolleybus 1) in both the version in force from 2026-07-01 and the
superseded one from 2026-01-01, cut down to a few journeys each but with their
``ValidDayBits`` untouched. Everything asserted below was checked against the
whole file first, so a fixture that drifts from reality fails here rather than
quietly agreeing with itself.
"""

import datetime as dt
import zipfile
from pathlib import Path

from dpmp_gtfs.cis.calendars import build_calendars

FIXTURES = Path(__file__).parent / "fixtures" / "netex"
CURRENT = FIXTURES / "line-655001-2026-07-01.xml"
SUPERSEDED = FIXTURES / "line-655001-2026-01-01.xml"

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
