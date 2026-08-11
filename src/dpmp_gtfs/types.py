"""Data structures shared between modules.

Kept apart from the code that produces them so that consumers do not have to
import a producer to name its output -- the writer needs to know what a Feed
looks like, not how the builder assembles one. Everything here is data:
behaviour lives with the module that owns it.
"""

import datetime as dt
import hashlib
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

    Almost entirely from the API: which trips a line has comes from walking its
    trip-number space, which way each one runs from the stop order of the trips
    just fetched. See :mod:`dpmp_gtfs.static.discovery` and
    :mod:`dpmp_gtfs.static.direction`. Days of operation are the one exception
    -- those come from CIS, because the API's are wrong.
    """

    stops: list[ApiStop]
    lines: list[Line]
    directions: dict[tuple[str, int], int] = field(default_factory=dict)
    """``(line_id, connection_id)`` -> ``direction_id``, derived from stop order."""
    connections: dict[tuple[str, int], Connection] = field(default_factory=dict)
    """``(line_id, connection_id)`` -> the trip's stop times, from the API."""
    calendars: dict[tuple[str, int], TripCalendar] = field(default_factory=dict)
    """``(line jdf id, connection_id)`` -> the days that trip runs, from CIS.

    Keyed by the JDF line number rather than the API's ``lineId``, because that
    is the only line identifier the two sources share."""

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


@dataclass(frozen=True, slots=True)
class TripCalendar:
    """The days one trip runs on, and where in CIS they came from."""

    days: frozenset[dt.date]
    """Restricted to the feed's window, so this shrinks from the front every
    night as the window slides forward."""
    origin: str
    """A stable name for the calendar these days were read from.

    Everything else about a service is window-relative, and a service id built
    out of window-relative things is rewritten nightly for no reason. This is
    not: it identifies the ``DayType`` bitmaps in the archive that the days came
    from, which are fixed until DPMP files a new timetable. It is what keeps two
    services that share a weekly pattern apart without either of them being
    renamed as the window moves. Empty when the days did not come from CIS."""


DAY_NAMES = ("mo", "tu", "we", "th", "fr", "sa", "su")
WORKING_WEEK = frozenset({0, 1, 2, 3, 4})
LISTED_DAYS = "dates"
"""What a service with no weekly pattern is called.

Deliberately says nothing about weekdays. Such a service is described entirely
by the dates in ``calendar_dates.txt``, and those are whatever part of it still
falls inside the feed's window -- naming it after them would rename it every
night as the window slid forward and the earliest of them dropped off."""


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
    added: frozenset[dt.date] = frozenset()
    """Days the service runs on beyond its weekly pattern."""
    removed: frozenset[dt.date] = frozenset()
    """Days the pattern says it runs on but it does not.

    Both exist because the real calendars come from CIS as a day-by-day bitmap,
    and DPMP runs three different weekday timetables depending on whether
    schools are in session -- a difference no set of seven weekdays can carry.
    """
    origin: str = ""
    """Which CIS calendar the exceptions were read from -- see
    :attr:`TripCalendar.origin`.

    Carried only by services that have exceptions, and only so that they can be
    told apart: term time and the school holidays are both ``wd`` and differ
    only in the days they take off. Services without exceptions leave it empty
    and keep sharing one calendar row across the whole network, which is the
    ordinary case and by far the commonest.
    """

    @property
    def runs_at_all(self) -> bool:
        """Whether anything at all puts this service on the road."""
        return bool(self.days or self.holidays or self.added)

    @property
    def base_id(self) -> str:
        """The weekly pattern as a name: ``wd``, ``sa-su+h``, ``mo-fr``.

        The whole working week collapses to ``wd`` however it was spelled, so a
        trip marked ``X`` and one marked ``1,2,3,4,5`` share a calendar row
        rather than producing two rows saying the same thing.

        A service with no weekly pattern -- the trips that run only for the
        last weeks of a timetable period, too few days for any weekday to carry
        a majority -- is :data:`LISTED_DAYS`, and nothing more. Everything
        those services have to be named after moves with the feed's window.
        """
        remaining = set(self.days)
        parts = []
        if remaining >= WORKING_WEEK:
            parts.append("wd")
            remaining -= WORKING_WEEK
        parts.extend(DAY_NAMES[day] for day in sorted(remaining))

        name = "-".join(parts) or (LISTED_DAYS if self.added else "")
        if self.holidays:
            # Always marked, so that "+" and "7" can never land on one id while
            # meaning different things.
            name = f"{name}+h" if name else "h"
        if not name:
            raise ValueError("service runs on no days at all")
        return name

    @property
    def service_id(self) -> str:
        """A legible name: ``wd``, ``sa-su+h``, ``mo-fr``, ``wd-a3f21c``.

        The suffix is a digest of :attr:`origin` rather than of anything about
        the days themselves. Two builds a night apart describe the same service
        over windows that differ by a day, so a suffix drawn from the days --
        a hash of them, their last one, or a position in a list ordered by them
        -- renames services that have not changed. The archive the days were
        read from does not move.
        """
        if not self.origin:
            return self.base_id
        digest = hashlib.blake2s(self.origin.encode(), digest_size=3).hexdigest()
        return f"{self.base_id}-{digest}"

    @property
    def weekday_flags(self) -> tuple[int, int, int, int, int, int, int]:
        """Monday..Sunday, as the 0/1 columns of ``calendar.txt``."""
        flags = tuple(int(day in self.days) for day in range(7))
        return flags  # type: ignore[return-value]

    def runs_on(self, day: dt.date, *, holiday: bool) -> bool:
        """Whether this service operates on a given date.

        On a state holiday the network runs its Sunday timetable, so what
        decides the day is :attr:`holidays` rather than the weekday the holiday
        happens to fall on. An explicit exception outranks both: it is the one
        thing said about that date and nothing else, so nothing else can
        override it.
        """
        if day in self.added:
            return True
        if day in self.removed:
            return False
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
