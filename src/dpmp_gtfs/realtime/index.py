"""Lookup structures that connect realtime observations to the static feed.

``/api/buses`` reports ``line_name`` and ``connection_no``, which are exactly
the upstream's own line and trip numbers -- the same pair the timetable
endpoints use. So the join is direct, with no name matching or heuristics.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from dpmp_gtfs.ids import trip_id


@dataclass(frozen=True, slots=True)
class ScheduledStop:
    stop_id: str
    station: int
    """Station number without platform -- what ``last_stop_number`` reports."""
    sequence: int
    seconds: int
    """Scheduled time as seconds from the start of the service day. May exceed
    86400 for trips that cross midnight."""


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


class StaticIndex:
    """Trips of the current static feed, addressable the way realtime sees them."""

    def __init__(self, trips: dict[str, ScheduledTrip]) -> None:
        self._by_trip_id = trips

    def __len__(self) -> int:
        return len(self._by_trip_id)

    def lookup(self, line: int, connection: int) -> ScheduledTrip | None:
        return self._by_trip_id.get(trip_id(line, connection))

    @classmethod
    def from_zip(cls, path: Path) -> StaticIndex:
        """Read the index straight out of a built ``gtfs.zip``.

        Reading the published artefact rather than rebuilding from the API
        guarantees the realtime feed references trips that consumers can
        actually resolve: if the two ever disagree, the feed is broken.
        """
        with zipfile.ZipFile(path) as zf:

            def rows(name: str) -> list[dict[str, str]]:
                with zf.open(name) as fh:
                    return list(csv.DictReader(io.TextIOWrapper(fh, "utf8")))

            routes_by_trip = {r["trip_id"]: r["route_id"] for r in rows("trips.txt")}

            collected: dict[str, list[ScheduledStop]] = {}
            for row in rows("stop_times.txt"):
                collected.setdefault(row["trip_id"], []).append(
                    ScheduledStop(
                        stop_id=row["stop_id"],
                        station=_station_of(row["stop_id"]),
                        sequence=int(row["stop_sequence"]),
                        seconds=_parse_gtfs_time(row["departure_time"]),
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


def _station_of(stop_id: str) -> int:
    """``"S16P2"`` -> ``16``."""
    return int(stop_id.removeprefix("S").split("P")[0])


def _parse_gtfs_time(value: str) -> int:
    """``"24:23:00"`` -> seconds. Hours past 24 are meaningful here."""
    hours, minutes, seconds = (int(p) for p in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds
