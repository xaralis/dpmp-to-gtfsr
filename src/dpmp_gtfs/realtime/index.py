"""Lookup structures that connect realtime observations to the static feed.

``/api/buses`` reports ``line_name`` and ``connection_no``, which are exactly
the upstream's own line and trip numbers -- the same pair the timetable
endpoints use. So the join is direct, with no name matching or heuristics.
"""

from dataclasses import dataclass
from pathlib import Path

from dpmp_gtfs.archive import read_tables
from dpmp_gtfs.ids import trip_id


class StaticIndex:
    """Trips of the current static feed, addressable the way realtime sees them."""

    def __init__(self, trips: dict[str, ScheduledTrip]) -> None:
        self._by_trip_id = trips

    def __len__(self) -> int:
        return len(self._by_trip_id)

    def lookup(self, line: int | None, connection: int) -> ScheduledTrip | None:
        """The trip a vehicle is running, if it is one this feed knows.

        ``line`` is ``None`` for a vehicle whose line number the upstream sent
        as something other than a number; there is nothing to look up, and the
        caller already skips a vehicle it cannot place.
        """
        if line is None:
            return None
        return self._by_trip_id.get(trip_id(line, connection))

    @classmethod
    def from_zip(cls, path: Path) -> StaticIndex:
        """Read the index straight out of a built ``gtfs.zip``.

        Reading the published artefact rather than rebuilding from the API
        guarantees the realtime feed references trips that consumers can
        actually resolve: if the two ever disagree, the feed is broken.
        """
        # stops.txt only supplies display names; a feed without it is still
        # perfectly usable for matching realtime to trips.
        tables = read_tables(path, "trips.txt", "stops.txt", "stop_times.txt")

        routes_by_trip = {r["trip_id"]: r["route_id"] for r in tables["trips.txt"]}
        names = {r["stop_id"]: r["stop_name"] for r in tables["stops.txt"]}

        collected: dict[str, list[ScheduledStop]] = {}
        for row in tables["stop_times.txt"]:
            collected.setdefault(row["trip_id"], []).append(
                ScheduledStop(
                    stop_id=row["stop_id"],
                    station=_station_of(row["stop_id"]),
                    sequence=int(row["stop_sequence"]),
                    seconds=_parse_gtfs_time(row["departure_time"]),
                    name=names.get(row["stop_id"], ""),
                )
            )

        trips = {
            tid: ScheduledTrip(
                trip_id=tid,
                route_id=routes_by_trip.get(tid, ""),
                stops=tuple(sorted(stops, key=lambda s: s.sequence)),
            )
            for tid, stops in collected.items()
        }
        return cls(trips)


@dataclass(frozen=True, slots=True)
class ScheduledTrip:
    trip_id: str
    route_id: str
    stops: tuple[ScheduledStop, ...]

    def index_of_station(self, station: int) -> int | None:
        """Position of a station within this trip, if it calls there.

        A station can legitimately appear twice on a circular trip; the first
        match is returned, which is the conservative reading when a vehicle has
        only just reached it.
        """
        for i, stop in enumerate(self.stops):
            if stop.station == station:
                return i
        return None

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


def _station_of(stop_id: str) -> int:
    """``"S16P2"`` -> ``16``."""
    return int(stop_id.removeprefix("S").split("P")[0])


def _parse_gtfs_time(value: str) -> int:
    """``"24:23:00"`` -> seconds. The inverse of
    :func:`dpmp_gtfs.timeutil.format_gtfs_time`, so hours past 24 stay
    meaningful rather than wrapping."""
    hours, minutes, seconds = (int(p) for p in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds
