"""Data structures shared between modules.

Kept apart from the code that produces them so that consumers do not have to
import a producer to name its output -- the writer needs to know what a Feed
looks like, not how the builder assembles one. Everything here is data:
behaviour lives with the module that owns it.
"""

import datetime as dt
from dataclasses import dataclass, field

from dpmp_gtfs.api.models import Code, ConnectionDetail, ConnectionSummary, Line, Station

type Point = tuple[float, float]
"""Latitude, longitude. GeoJSON reverses this; nothing else here does."""

type StopSequence = tuple[str, ...]
"""Stop ids in travel order. Identifies a shape: every trip calling at the
same stops in the same order shares one."""


# --- timetable --------------------------------------------------------------


@dataclass(slots=True)
class Timetable:
    """Everything needed to build a static feed, as crawled from the API."""

    stations: list[Station]
    lines: list[Line]
    codes: list[Code]
    summaries: dict[tuple[int, int], ConnectionSummary] = field(default_factory=dict)
    """Keyed by ``(line_number, connection_number)``."""
    details: dict[tuple[int, int], ConnectionDetail] = field(default_factory=dict)

    @property
    def trip_count(self) -> int:
        return len(self.details)


# --- geometry ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shape:
    """One routed line, shared by every trip with the same stop sequence."""

    shape_id: str
    points: tuple[Point, ...]
    """Latitude/longitude along the road."""
    point_distances: tuple[float, ...]
    """Cumulative metres at each point."""
    stop_distances: tuple[float, ...]
    """Cumulative metres at each stop, for ``shape_dist_traveled``."""


# --- calendar ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Service:
    """A GTFS service: which weekdays a set of trips runs on."""

    working_days: bool
    saturday: bool
    sunday: bool

    @property
    def service_id(self) -> str:
        parts = [
            name
            for flag, name in (
                (self.working_days, "wd"),
                (self.saturday, "sa"),
                (self.sunday, "su"),
            )
            if flag
        ]
        if not parts:
            raise ValueError("service runs on no days at all")
        return "-".join(parts)

    @property
    def weekday_flags(self) -> tuple[int, int, int, int, int, int, int]:
        """Monday..Sunday, as the 0/1 columns of ``calendar.txt``."""
        wd = int(self.working_days)
        return (wd, wd, wd, wd, wd, int(self.saturday), int(self.sunday))

    def runs_on(self, day: dt.date, *, holiday: bool) -> bool:
        """Whether this service operates on a given date.

        A public holiday takes the Sunday timetable regardless of which weekday
        it falls on -- exactly what code 5 states ("runs on Sundays and
        state-recognised holidays").
        """
        if holiday:
            return self.sunday
        weekday = day.weekday()
        if weekday == 5:
            return self.saturday
        if weekday == 6:
            return self.sunday
        return self.working_days


@dataclass(frozen=True, slots=True)
class CalendarException:
    """One ``calendar_dates.txt`` row: a date a service does or does not run."""

    service_id: str
    date: dt.date
    added: bool
    """True for GTFS exception_type 1 (added), False for 2 (removed)."""


# --- GTFS records -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stop:
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    location_type: int
    parent_station: str
    platform_code: str
    wheelchair_boarding: int


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    route_short_name: str
    route_long_name: str
    route_type: int


@dataclass(frozen=True, slots=True)
class Trip:
    route_id: str
    service_id: str
    trip_id: str
    trip_headsign: str
    direction_id: int
    wheelchair_accessible: int
    shape_id: str = ""
    """Empty when geometry could not be routed. GTFS allows trips without one."""


@dataclass(frozen=True, slots=True)
class StopTime:
    trip_id: str
    arrival_time: str
    departure_time: str
    stop_id: str
    stop_sequence: int
    pickup_type: int
    drop_off_type: int
    shape_dist_traveled: str = ""
    """Metres along the shape. Blank when the trip has no shape."""


@dataclass(slots=True)
class Feed:
    """A complete static feed, ready to serialise."""

    stops: list[Stop] = field(default_factory=list)
    shapes: list[Shape] = field(default_factory=list)
    unserved_stops: dict[str, str] = field(default_factory=dict)
    """Stop id -> name for stops excluded because nothing calls at them. Kept
    so rebuilds can spot diversions starting and ending."""
    routes: list[Route] = field(default_factory=list)
    trips: list[Trip] = field(default_factory=list)
    stop_times: list[StopTime] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    calendar_exceptions: list[CalendarException] = field(default_factory=list)
    start_date: dt.date = field(default_factory=dt.date.today)
    end_date: dt.date = field(default_factory=dt.date.today)
