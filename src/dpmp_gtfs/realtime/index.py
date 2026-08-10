"""Lookup structures that connect realtime observations to the static feed.

``/{provider}/vehicles`` reports ``lineId`` and ``connectionId``, which are
exactly the upstream's own line and trip identifiers -- the same pair the
timetable endpoints use. So the join is direct, with no name matching or
heuristics.
"""

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from dpmp_gtfs.archive import read_tables
from dpmp_gtfs.ids import station_id, trip_id
from dpmp_gtfs.timeutil import DAY, PRAGUE


class StaticIndex:
    """Trips of the current static feed, addressable the way realtime sees them."""

    def __init__(
        self, trips: dict[str, ScheduledTrip], calendar: ServiceCalendar | None = None
    ) -> None:
        self._by_trip_id = trips
        self._calendar = calendar or ServiceCalendar()

        # Departures per stop, in timetable order. Built once: a stop board
        # would otherwise scan all 45,000 stop times on every request.
        #
        # Filed under the platform *and* its parent station. A passenger
        # standing at an interchange wants everything leaving from it, and the
        # map offers the station as the only thing to click until a line is
        # picked -- so a board that only answered for platforms answered for
        # nothing most of the time.
        self._by_stop: dict[str, list[tuple[int, str, ScheduledStop]]] = {}
        for trip in trips.values():
            for stop in trip.stops[:-1]:  # nobody departs from the last stop
                entry = (stop.seconds, trip.trip_id, stop)
                self._by_stop.setdefault(stop.stop_id, []).append(entry)
                self._by_stop.setdefault(station_id(stop.station), []).append(entry)
        for board in self._by_stop.values():
            board.sort(key=lambda e: (e[0], e[1]))

        # Under the station id as well, so a station board can be titled. The
        # platforms of a station all carry its name, so either is fine.
        self._stop_names: dict[str, str] = {}
        for trip in trips.values():
            for stop in trip.stops:
                self._stop_names[stop.stop_id] = stop.name
                self._stop_names.setdefault(station_id(stop.station), stop.name)

    def __len__(self) -> int:
        return len(self._by_trip_id)

    def lookup(self, line: str, connection: int) -> ScheduledTrip | None:
        """The trip a vehicle is running, if it is one this feed knows."""
        return self._by_trip_id.get(trip_id(line, connection))

    def stop_name(self, stop_id: str) -> str:
        """The name a passenger knows a stop by, or empty if it is unknown."""
        return self._stop_names.get(stop_id, "")

    def departures(
        self, stop_id: str, now: dt.datetime, limit: int = 8
    ) -> list[tuple[dt.datetime, ScheduledTrip, ScheduledStop]]:
        """The next scheduled departures from one stop, soonest first.

        Accepts either a platform (``S23P2``) or a whole station (``S23``);
        a station board merges every platform under it.

        Returns the absolute local departure time, the trip, and the stop it
        leaves from -- which is not necessarily the one that was asked for.

        Two service days are considered, not one. A trip written 24:30 belongs
        to yesterday's service day but departs after midnight tonight, so a
        board built only from today's would lose the night lines exactly when
        they are the only thing running.
        """
        board = self._by_stop.get(stop_id)
        if not board:
            return []

        local = now.astimezone(PRAGUE)
        found: list[tuple[dt.datetime, ScheduledTrip, ScheduledStop]] = []

        for days_back in (1, 0):
            service_day = local.date() - dt.timedelta(days=days_back)
            midnight = dt.datetime.combine(service_day, dt.time(), tzinfo=PRAGUE)
            elapsed = (local - midnight).total_seconds()

            for seconds, tid, stop in board:
                if seconds < elapsed:
                    continue
                if seconds - elapsed > DAY:
                    break  # the board is sorted; nothing later is nearer
                trip = self._by_trip_id[tid]
                if not self._calendar.runs_on(trip.service_id, service_day):
                    continue
                found.append((midnight + dt.timedelta(seconds=seconds), trip, stop))

        found.sort(key=lambda d: d[0])
        return found[:limit]

    @classmethod
    def from_zip(cls, path: Path) -> StaticIndex:
        """Read the index straight out of a built ``gtfs.zip``.

        Reading the published artefact rather than rebuilding from the API
        guarantees the realtime feed references trips that consumers can
        actually resolve: if the two ever disagree, the feed is broken.
        """
        # stops.txt only supplies display names; a feed without it is still
        # perfectly usable for matching realtime to trips.
        tables = read_tables(
            path,
            "trips.txt",
            "stops.txt",
            "stop_times.txt",
            "routes.txt",
            "calendar.txt",
            "calendar_dates.txt",
        )

        by_trip = {r["trip_id"]: r for r in tables["trips.txt"]}
        names = {r["stop_id"]: r["stop_name"] for r in tables["stops.txt"]}
        positions = {
            r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"])) for r in tables["stops.txt"]
        }
        lines = {r["route_id"]: r["route_short_name"] for r in tables["routes.txt"]}

        collected: dict[str, list[ScheduledStop]] = {}
        for row in tables["stop_times.txt"]:
            collected.setdefault(row["trip_id"], []).append(
                ScheduledStop(
                    stop_id=row["stop_id"],
                    station=_station_of(row["stop_id"]),
                    sequence=int(row["stop_sequence"]),
                    seconds=_parse_gtfs_time(row["departure_time"]),
                    name=names.get(row["stop_id"], ""),
                    position=positions.get(row["stop_id"]),
                )
            )

        trips = {
            tid: ScheduledTrip(
                trip_id=tid,
                route_id=by_trip.get(tid, {}).get("route_id", ""),
                stops=tuple(sorted(stops, key=lambda s: s.sequence)),
                service_id=by_trip.get(tid, {}).get("service_id", ""),
                headsign=by_trip.get(tid, {}).get("trip_headsign", ""),
                line=lines.get(by_trip.get(tid, {}).get("route_id", ""), ""),
            )
            for tid, stops in collected.items()
        }
        calendar = ServiceCalendar.from_rows(tables["calendar.txt"], tables["calendar_dates.txt"])
        return cls(trips, calendar)


@dataclass(frozen=True, slots=True)
class ScheduledTrip:
    trip_id: str
    route_id: str
    stops: tuple[ScheduledStop, ...]
    service_id: str = ""
    headsign: str = ""
    """Where the trip finishes -- which is what a passenger reads as its
    direction along the line."""
    line: str = ""
    """``route_short_name``, the number painted on the vehicle."""

    def index_of_station(self, station: int, before: int | None = None) -> int | None:
        """Position of a station within this trip, if it calls there.

        Out-and-back trips serve the same station twice -- 234 of 2,728 trips
        do, and line 8 visits twelve stations twice -- so "which call is this"
        has to be answered, not guessed. ``last_stop_number`` carries no
        platform to tell them apart, but the vehicle's current position does,
        so ``before`` narrows it to the most recent call the vehicle can
        actually have made.

        Without that this returned the first call every time, and a vehicle on
        its return leg was reported against a schedule from the outward one:
        an hour of delay that never happened.
        """
        matches = [i for i, stop in enumerate(self.stops) if stop.station == station]
        if not matches:
            return None
        if before is None:
            return matches[0]
        earlier = [i for i in matches if i < before]
        return earlier[-1] if earlier else matches[0]

    def locate(self, stop_id: str | None, station: int | None) -> int | None:
        """Where along this trip a vehicle reporting that stop currently is.

        Platform first, then the station it belongs to. The fallback is not
        defensive padding: vehicles do report a platform their trip does not
        call at while the station itself is plainly on the route, and treating
        that as "not on this trip" would drop the vehicle's predictions
        entirely rather than place it one platform out.
        """
        for i, stop in enumerate(self.stops):
            if stop.stop_id == stop_id:
                return i
        return None if station is None else self.index_of_station(station)


@dataclass(frozen=True, slots=True)
class ServiceCalendar:
    """Which services run on which dates, read back out of the published feed.

    A departure board needs this and the realtime join does not, which is why
    the index went without it for so long: a vehicle reports the trip it is
    running, so no calendar is involved. Asking "what leaves here next" is the
    opposite problem -- the answer is drawn from the timetable, and a timetable
    without a calendar would offer Sunday trips on a Tuesday.
    """

    weekdays: dict[str, tuple[bool, ...]] = field(default_factory=dict)
    """Service id -> seven flags, Monday first."""
    window: dict[str, tuple[dt.date, dt.date]] = field(default_factory=dict)
    exceptions: dict[tuple[str, dt.date], bool] = field(default_factory=dict)
    """``(service, date) -> runs``. Holidays live here: the network keeps its
    Sunday timetable on them, which the weekday flags alone cannot express."""

    def runs_on(self, service_id: str, day: dt.date) -> bool:
        """Whether a service operates on a date, exceptions taking precedence."""
        if (override := self.exceptions.get((service_id, day))) is not None:
            return override
        flags = self.weekdays.get(service_id)
        if flags is None:
            # An unknown service is assumed to run. A feed that has calendar
            # data will not hit this; one that does not should still show a
            # board rather than an empty one.
            return True
        start, end = self.window.get(service_id, (day, day))
        return start <= day <= end and flags[day.weekday()]

    @classmethod
    def from_rows(
        cls, calendar: list[dict[str, str]], calendar_dates: list[dict[str, str]]
    ) -> ServiceCalendar:
        days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        weekdays = {r["service_id"]: tuple(r[d] == "1" for d in days) for r in calendar}
        window = {
            r["service_id"]: (_parse_date(r["start_date"]), _parse_date(r["end_date"]))
            for r in calendar
        }
        # exception_type 1 adds a date, 2 removes one.
        exceptions = {
            (r["service_id"], _parse_date(r["date"])): r["exception_type"] == "1"
            for r in calendar_dates
        }
        return cls(weekdays=weekdays, window=window, exceptions=exceptions)


@dataclass(frozen=True, slots=True)
class ScheduledStop:
    stop_id: str
    station: int
    """Station number without platform -- what ``last_stop_number`` reports."""
    sequence: int
    seconds: int
    """Scheduled time as seconds from the start of the service day. May exceed
    86400 for trips that cross midnight."""
    name: str = ""
    """Human-readable stop name. Carried here so callers that need to *show* a
    vehicle's position -- rather than just reference it -- do not have to reopen
    the feed to turn an id into something a passenger recognises."""
    position: tuple[float, float] | None = None
    """Latitude/longitude, for working out which way a vehicle is heading."""


def _parse_date(value: str) -> dt.date:
    """``"20260808"`` -> a date. GTFS writes dates without separators."""
    return dt.datetime.strptime(value, "%Y%m%d").date()


def _station_of(stop_id: str) -> int:
    """``"S16P2"`` -> ``16``."""
    return int(stop_id.removeprefix("S").split("P")[0])


def _parse_gtfs_time(value: str) -> int:
    """``"24:23:00"`` -> seconds. The inverse of
    :func:`dpmp_gtfs.timeutil.format_gtfs_time`, so hours past 24 stay
    meaningful rather than wrapping."""
    hours, minutes, seconds = (int(p) for p in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds
