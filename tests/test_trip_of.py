"""Which trip a vehicle is on, given that its number cannot be trusted.

The pair behind these tests is real. On 2026-11-07 a vehicle on line 1 reported
``connectionId`` 383, due at Jesničánky,točna at 20:41. The API's trip 383 agrees.
In CIS trip 383 is another journey an hour earlier, and the 20:41 is trip 397.
"""

import datetime as dt

from dpmp_gtfs.api.models import Vehicle
from dpmp_gtfs.realtime.index import ScheduledStop, ScheduledTrip, ServiceCalendar, StaticIndex
from dpmp_gtfs.timeutil import PRAGUE

JESNICANKY = 7
SLOVANY = 9
AT_20_41 = 20 * 3600 + 41 * 60
SATURDAY = dt.datetime(2026, 11, 7, 20, 40, tzinfo=PRAGUE)
MONDAY = dt.datetime(2026, 11, 9, 20, 40, tzinfo=PRAGUE)
SHORTLY_AFTER_MIDNIGHT = dt.datetime(2026, 11, 8, 0, 10, tzinfo=PRAGUE)


def _trip(trip_id: str, *calls: tuple[int, int], service: str = "weekend") -> ScheduledTrip:
    return ScheduledTrip(
        trip_id=trip_id,
        route_id="L1",
        line="1",
        service_id=service,
        headsign="Slovany,točna",
        stops=tuple(
            ScheduledStop(f"S{station}", station, sequence, seconds)
            for sequence, (station, seconds) in enumerate(calls)
        ),
    )


def _index(*trips: ScheduledTrip) -> StaticIndex:
    calendar = ServiceCalendar(
        weekdays={
            "weekend": (False, False, False, False, False, True, True),
            "weekday": (True, True, True, True, True, False, False),
        },
        window={
            "weekend": (dt.date(2026, 11, 1), dt.date(2027, 11, 1)),
            "weekday": (dt.date(2026, 11, 1), dt.date(2027, 11, 1)),
        },
    )
    return StaticIndex({t.trip_id: t for t in trips}, calendar)


def _vehicle(number: int, stop: int | None = JESNICANKY, due: str | None = "20:41:00") -> Vehicle:
    return Vehicle.model_validate(
        {
            "vid": "1",
            "lineId": "1",
            "connectionId": number,
            "gpsLat": 50.0,
            "gpsLon": 15.0,
            "destinationName": "Slovany,točna",
            "nextStopId": stop,
            "nextStopScheduledDepartureTime": due,
        }
    )


AN_HOUR_EARLIER = _trip("L1C383", (JESNICANKY, AT_20_41 - 3600), (SLOVANY, AT_20_41 - 2400))
THE_ONE_DUE = _trip("L1C397", (JESNICANKY, AT_20_41), (SLOVANY, AT_20_41 + 1080))


def test_a_number_cis_gives_to_another_journey_does_not_win():
    """Taking the number at its word reports the vehicle an hour late."""
    assert _index(AN_HOUR_EARLIER, THE_ONE_DUE).trip_of(_vehicle(383), SATURDAY) is THE_ONE_DUE


def test_a_number_that_agrees_settles_a_tie():
    twin = _trip("L1C398", (JESNICANKY, AT_20_41), (SLOVANY, AT_20_41 + 1200))

    assert _index(THE_ONE_DUE, twin).trip_of(_vehicle(398), SATURDAY) is twin


def test_a_trip_that_does_not_run_today_is_not_a_candidate():
    monday = _trip("L1C400", (JESNICANKY, AT_20_41), (SLOVANY, AT_20_41 + 1080), service="weekday")
    index = _index(THE_ONE_DUE, monday)

    assert index.trip_of(_vehicle(383), SATURDAY) is THE_ONE_DUE
    assert index.trip_of(_vehicle(383), MONDAY) is monday


def test_the_destination_settles_what_the_number_cannot():
    elsewhere = ScheduledTrip(
        trip_id="L1C500",
        route_id="L1",
        line="1",
        service_id="weekend",
        headsign="Dražkovice",
        stops=(ScheduledStop(f"S{JESNICANKY}", JESNICANKY, 0, AT_20_41),),
    )

    assert _index(THE_ONE_DUE, elsewhere).trip_of(_vehicle(1), SATURDAY) is THE_ONE_DUE


def test_nothing_recognisable_is_left_out_rather_than_guessed():
    """A vehicle attached to the wrong trip is worse than one that is missing."""
    index = _index(AN_HOUR_EARLIER)

    assert index.trip_of(_vehicle(383), SATURDAY) is None


def test_a_call_past_midnight_belongs_to_the_day_before():
    """Saturday's 24:18 is 00:18 on Sunday's clock. Sunday also runs the weekend
    service, so use a service that runs on Saturday only to see the difference."""
    late = _trip("L1C322", (JESNICANKY, 24 * 3600 + 18 * 60), (SLOVANY, 24 * 3600 + 23 * 60))
    calendar = ServiceCalendar(
        weekdays={"weekend": (False, False, False, False, False, True, False)},
        window={"weekend": (dt.date(2026, 11, 1), dt.date(2027, 11, 1))},
    )
    index = StaticIndex({late.trip_id: late}, calendar)

    found = index.trip_of(_vehicle(1, due="00:18:00"), SHORTLY_AFTER_MIDNIGHT)

    assert found is late


def test_a_vehicle_that_does_not_say_where_it_is_due_has_only_its_number():
    index = _index(AN_HOUR_EARLIER, THE_ONE_DUE)

    assert index.trip_of(_vehicle(383, due=None), SATURDAY) is AN_HOUR_EARLIER
    assert index.trip_of(_vehicle(383, stop=None), SATURDAY) is AN_HOUR_EARLIER
