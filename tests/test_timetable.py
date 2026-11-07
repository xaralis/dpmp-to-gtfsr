"""Tests for assembling the timetable from CIS journeys and API stops."""

import datetime as dt
import logging

import pytest

from dpmp_gtfs.api.models import Line, Stop
from dpmp_gtfs.cis import Call, CisTimetable, Journey
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.ids import station_id
from dpmp_gtfs.static.builder import build_feed
from dpmp_gtfs.static.timetable import assemble, load_timetable
from dpmp_gtfs.types import TripCalendar

STOPS = [
    Stop(id=1, name="Slovany,točna", gps_latitude=50.0, gps_longitude=15.0),
    Stop(id=2, name="Slovany,u přejezdu", gps_latitude=50.1, gps_longitude=15.1),
    Stop(id=3, name="ERA Zámeček", gps_latitude=50.2, gps_longitude=15.2),
    Stop(
        id=4, name="U Marka", aliases=["Autobusové nádraží"], gps_latitude=50.3, gps_longitude=15.3
    ),
]
LINES = [Line(id="1", jdf_id="655001")]
DAYS = frozenset({dt.date(2026, 11, 9)})


def _journey(*names: str, number: int = 348, direction: int = 1) -> Journey:
    calls = tuple(
        Call(stop_name=name, arrival=f"20:0{i}:00", departure=f"20:0{i}:00")
        for i, name in enumerate(names)
    )
    return Journey(number=number, calls=calls, direction=direction, low_floor=True)


def _cis(journey: Journey, jdf_id: str = "655001") -> CisTimetable:
    key = (jdf_id, journey.number)
    return CisTimetable(
        calendars={key: TripCalendar(days=DAYS, origin="x")}, journeys={key: journey}
    )


def test_stops_are_found_by_name_however_the_two_sources_punctuate_it():
    """Measured: of 218 CIS names, every one differing from /stops differs only
    in commas, spaces and case, or is one of its aliases."""
    cis = _cis(_journey("Slovany,točna", "Slovany,U Přejezdu", "ERA,Zámeček", "Autobusové nádraží"))

    table = assemble(STOPS, LINES, cis)

    assert [s.stop_id for s in table.connections[("1", 348)].stops] == [1, 2, 3, 4]


def test_a_stop_the_api_does_not_publish_loses_only_that_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        table = assemble(STOPS, LINES, _cis(_journey("Slovany,točna", "Atlantis", "U Marka")))

    assert [s.stop_id for s in table.connections[("1", 348)].stops] == [1, 4]
    assert "'Atlantis'" in caplog.text


def test_lines_dpmp_does_not_list_are_left_out():
    """CIS carries 902 and 906. Neither /lines nor DPMP's own list of lines has
    them, and publishing them doubled line 2's departures."""
    table = assemble(STOPS, LINES, _cis(_journey("Slovany,točna", "U Marka"), jdf_id="655902"))

    assert table.connections == {}
    assert table.calendars == {}


def test_a_listed_line_cis_does_not_know_says_so(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assemble(STOPS, LINES, CisTimetable())

    assert "line 1 (655001) has no journeys in CIS" in caplog.text


def test_journey_details_survive_into_the_feed():
    journey = Journey(
        number=348,
        calls=(
            Call("Slovany,točna", None, "20:02:00"),
            Call("U Marka", "20:03:00", "20:03:00", on_request=True),
            Call("ERA,Zámeček", "20:04:00", None),
        ),
        direction=1,
        low_floor=True,
    )

    feed = build_feed(assemble(STOPS, LINES, _cis(journey)), start_date=dt.date(2026, 11, 9))

    assert feed.trips[0].trip_id == "L1C348"
    assert feed.trips[0].direction_id == 1
    assert feed.trips[0].wheelchair_accessible == 1
    assert [t.pickup_type for t in feed.stop_times] == [0, 3, 0]
    assert [t.departure_time for t in feed.stop_times] == ["20:02:00", "20:03:00", "20:04:00"]


def test_a_stop_without_a_platform_is_a_stop_the_feed_can_call_at():
    """CIS does not know platforms. GTFS forbids a stop_time at a parent station,
    so these are plain stops rather than parents with an invented child."""
    feed = build_feed(
        assemble(STOPS, LINES, _cis(_journey("Slovany,točna", "U Marka"))),
        start_date=dt.date(2026, 11, 9),
    )

    assert {s.stop_id: s.location_type for s in feed.stops} == {
        station_id(1): 0,
        station_id(4): 0,
    }
    assert {t.stop_id for t in feed.stop_times} == {station_id(1), station_id(4)}
    assert set(feed.unserved_stops) == {station_id(2), station_id(3)}


def test_a_trip_a_later_timetable_has_gets_its_own_id():
    number = "348-20261114"
    journey = _journey("Slovany,točna", "U Marka")
    cis = CisTimetable(
        calendars={
            ("655001", 348): TripCalendar(days=DAYS, origin="a"),
            ("655001", number): TripCalendar(days=frozenset({dt.date(2026, 11, 16)}), origin="b"),
        },
        journeys={("655001", 348): journey, ("655001", number): journey},
    )

    feed = build_feed(assemble(STOPS, LINES, cis), start_date=dt.date(2026, 11, 9))

    assert sorted(t.trip_id for t in feed.trips) == ["L1C348", "L1C348-20261114"]


class FlakyApi:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.attempts = 0

    async def stops(self) -> list[Stop]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise DpmpApiError("upstream is down")
        return STOPS

    async def lines(self) -> list[Line]:
        return LINES


async def test_an_outage_is_retried():
    api = FlakyApi(failures=2)

    table = await load_timetable(api, _cis(_journey("Slovany,točna", "U Marka")), backoff=0)

    assert api.attempts == 3
    assert ("1", 348) in table.connections


async def test_a_sustained_outage_raises_rather_than_building_on_nothing():
    api = FlakyApi(failures=10)

    with pytest.raises(DpmpApiError, match="failed after 3 attempts"):
        await load_timetable(api, CisTimetable(), attempts=3, backoff=0)
