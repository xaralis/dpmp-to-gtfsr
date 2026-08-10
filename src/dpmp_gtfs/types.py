"""Data structures shared between modules.

Kept apart from the code that produces them so that consumers do not have to
import a producer to name its output -- the writer needs to know what a Feed
looks like, not how the builder assembles one. Everything here is data:
behaviour lives with the module that owns it.
"""

import datetime as dt
from dataclasses import dataclass, field

from dpmp_gtfs.api.models import Connection, Line
from dpmp_gtfs.api.models import Stop as ApiStop

type LatLon = tuple[float, float]
"""A position, latitude first -- the order the feed and the API both use.

Named for its order on purpose. GeoJSON wants the reverse (see
:data:`dpmp_gtfs.web.coverage.LonLat`), the two are the same type to a type
checker, and swapping them puts Pardubice in the Indian Ocean without any
error at all."""

type StopSequence = tuple[str, ...]
"""Stop ids in travel order. Identifies a :class:`TripGeometry`: every trip
calling at the same stops in the same order shares one."""


@dataclass(slots=True)
class Feed:
    """A complete static feed, ready to serialise."""

    stops: list[Stop] = field(default_factory=list)
    shapes: list[TripGeometry] = field(default_factory=list)
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


@dataclass(slots=True)
class Timetable:
    """Everything needed to build a static feed.

    Built entirely from the API: which trips a line has comes from walking
    its trip-number space, which way each one runs from the stop order of the
    trips just fetched. See :mod:`dpmp_gtfs.static.discovery` and
    :mod:`dpmp_gtfs.static.direction`.
    """

    stops: list[ApiStop]
    lines: list[Line]
    directions: dict[tuple[str, int], int] = field(default_factory=dict)
    """``(line_id, connection_id)`` -> ``direction_id``, derived from stop order."""
    connections: dict[tuple[str, int], Connection] = field(default_factory=dict)
    """``(line_id, connection_id)`` -> the trip's stop times, from the API."""

    @property
    def trip_count(self) -> int:
        return len(self.connections)


@dataclass(frozen=True, slots=True)
class TripGeometry:
    """The routed path trips follow, shared by every trip calling at the same
    stops in the same order.

    Not called ``Shape``: that is the GTFS file name, not a description, and it
    says nothing next to shapely's geometry types. Not ``TripLine`` either --
    ``Line`` already means a transit line here, so it would read as the line a
    trip runs on rather than the path it takes."""

    shape_id: str
    """Kept spelled the GTFS way, since it is written out as ``shape_id``."""
    points: tuple[LatLon, ...]
    """The path along the road."""
    point_distances: tuple[float, ...]
    """Cumulative metres at each point."""
    stop_distances: tuple[float, ...]
    """Cumulative metres at each stop, for ``shape_dist_traveled``."""


DAY_NAMES = ("mo", "tu", "we", "th", "fr", "sa", "su")
WORKING_WEEK = frozenset({0, 1, 2, 3, 4})


@dataclass(frozen=True, slots=True)
class Service:
    """A GTFS service: which weekdays a set of trips runs on.

    An arbitrary set of days rather than the weekday/Saturday/Sunday triple the
    network mostly uses, because JDF also has a code per weekday and the
    airport shuttle runs on the days flights leave -- Mondays and Fridays, say.
    """

    days: frozenset[int]
    """Weekdays it runs, numbered as ``date.weekday()``: 0 = Monday, 6 = Sunday."""
    holidays: bool
    """Whether it also runs on state holidays, whatever weekday they land on.

    Deliberately separate from ``6 in days``. JDF distinguishes ``+`` ("jede v
    neděli a ve státem uznané svátky") from ``7`` ("jede v neděli"), and
    collapsing the two would put a trip on the road on Christmas Day that its
    timetable says stays in the depot.
    """

    @property
    def service_id(self) -> str:
        """A legible name, e.g. ``wd``, ``sa-su+h``, ``mo-fr``.

        The whole working week collapses to ``wd`` however it was spelled, so a
        trip marked ``X`` and one marked ``1,2,3,4,5`` share a calendar row
        rather than producing two rows saying the same thing.
        """
        remaining = set(self.days)
        parts = []
        if remaining >= WORKING_WEEK:
            parts.append("wd")
            remaining -= WORKING_WEEK
        parts.extend(DAY_NAMES[day] for day in sorted(remaining))

        name = "-".join(parts)
        if self.holidays:
            # Always marked, so that "+" and "7" can never land on one id while
            # meaning different things.
            return f"{name}+h" if name else "h"
        if not name:
            raise ValueError("service runs on no days at all")
        return name

    @property
    def weekday_flags(self) -> tuple[int, int, int, int, int, int, int]:
        """Monday..Sunday, as the 0/1 columns of ``calendar.txt``."""
        flags = tuple(int(day in self.days) for day in range(7))
        return flags  # type: ignore[return-value]

    def runs_on(self, day: dt.date, *, holiday: bool) -> bool:
        """Whether this service operates on a given date.

        On a state holiday the network runs its Sunday timetable, so what
        decides the day is :attr:`holidays` rather than the weekday the holiday
        happens to fall on.
        """
        if holiday:
            return self.holidays
        return day.weekday() in self.days


@dataclass(frozen=True, slots=True)
class CalendarException:
    """One ``calendar_dates.txt`` row: a date a service does or does not run."""

    service_id: str
    date: dt.date
    added: bool
    """True for GTFS exception_type 1 (added), False for 2 (removed)."""


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
