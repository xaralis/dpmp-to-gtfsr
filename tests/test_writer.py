"""Tests for how a built feed is serialised."""

import datetime as dt
from pathlib import Path

from dpmp_gtfs.archive import read_tables
from dpmp_gtfs.static.calendar import calendar_exceptions
from dpmp_gtfs.static.writer import write_feed
from dpmp_gtfs.types import Feed, Service

TODAY = dt.date(2026, 8, 11)
HORIZON = TODAY + dt.timedelta(days=365)


def _feed(*services: Service) -> Feed:
    """A feed carrying nothing but calendars, with its exceptions derived the
    way :func:`build_feed` derives them rather than hand-written."""
    feed = Feed(services=list(services), start_date=TODAY, end_date=HORIZON)
    feed.calendar_exceptions = list(calendar_exceptions(feed.services, TODAY, HORIZON))
    return feed


def test_a_seasonal_service_is_left_out_of_calendar_txt(tmp_path: Path) -> None:
    """A service with no weekday is described entirely by calendar_dates.txt.

    A row of seven zeroes beside it would say the service never runs, which is
    both untrue and what makes validators flag the feed.
    """
    weekly = Service(days=frozenset({0, 1, 2, 3, 4}), holidays=False)
    seasonal = Service(
        days=frozenset(),
        holidays=False,
        added=frozenset({dt.date(2026, 8, 20), dt.date(2026, 8, 21)}),
    )

    destination = tmp_path / "gtfs.zip"
    write_feed(_feed(weekly, seasonal), destination)
    rows = read_tables(destination, "calendar.txt")["calendar.txt"]

    assert [r["service_id"] for r in rows] == [weekly.service_id]


def test_calendar_txt_survives_a_feed_of_nothing_but_seasonal_services(
    tmp_path: Path,
) -> None:
    """Its columns cannot be read off the first row when there is no first row.

    Reachable in the weeks a timetable period is running out, when every
    service left is describing its last few dates.
    """
    seasonal = Service(days=frozenset(), holidays=False, added=frozenset({dt.date(2026, 8, 20)}))

    destination = tmp_path / "gtfs.zip"
    write_feed(_feed(seasonal), destination)
    tables = read_tables(destination, "calendar.txt", "calendar_dates.txt")

    assert tables["calendar.txt"] == []
    assert [r["date"] for r in tables["calendar_dates.txt"]] == ["20260820"]
