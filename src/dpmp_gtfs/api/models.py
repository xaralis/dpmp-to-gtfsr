"""Typed models for the DPMP API responses.

Everything the upstream does oddly is normalised here, once, so that the rest
of the codebase never has to think about it again:

* ``state_dtime`` is **UTC**, even though every scheduled time in the API is
  local Europe/Prague. Parsed into an aware datetime below.
* ``current_stop_number`` encodes station *and* platform as ``station * 100 +
  platform``; ``last_stop_number`` carries the bare station number instead.
* ``time_difference`` is **not a delay** -- see :mod:`dpmp_gtfs.realtime.tracker`.
"""

import datetime as dt
import re
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, field_validator

from dpmp_gtfs.ids import stop_id
from dpmp_gtfs.upstream import whole_number

# --- shared field types -----------------------------------------------------

_HHMM = re.compile(r"^(\d{2})(\d{2})$")
_DURATION = re.compile(r"^(?P<sign>-?)(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})$")


def _empty_to_none(v: object) -> object:
    return None if v == "" else v


MaybeStr = Annotated[str | None, BeforeValidator(_empty_to_none)]


def parse_hhmm(value: str) -> dt.time:
    """``"0425"`` -> ``04:25``. The API uses this compact form for timetables."""
    m = _HHMM.match(value)
    if not m:
        raise ValueError(f"not a HHMM time: {value!r}")
    return dt.time(int(m.group(1)), int(m.group(2)))


def parse_duration(value: str) -> dt.timedelta:
    """``"-00:01:24"`` -> ``timedelta(seconds=-84)``.

    Note the sign handling: the whole magnitude is negated, which is what the
    old implementation got wrong by reaching for ``timedelta.seconds`` (a
    non-negative field) instead of ``total_seconds()``.
    """
    m = _DURATION.match(value)
    if not m:
        raise ValueError(f"not a signed HH:MM:SS duration: {value!r}")
    magnitude = dt.timedelta(
        hours=int(m.group("h")), minutes=int(m.group("m")), seconds=int(m.group("s"))
    )
    return -magnitude if m.group("sign") else magnitude


# --- /api/codes -------------------------------------------------------------


class Code(BaseModel):
    number: int
    name: str
    value: str


# --- /api/stations ----------------------------------------------------------


class Platform(BaseModel):
    number: int
    gps_latitude: float
    gps_longitude: float


class Station(BaseModel):
    number: int
    name: str
    platforms: list[Platform] = Field(default_factory=list)
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    codes: list[int] = Field(default_factory=list)


# --- /api/lines -------------------------------------------------------------


class LineStop(BaseModel):
    number: int
    name: str
    codes: list[int] = Field(default_factory=list)


class Line(BaseModel):
    number: int
    stops: list[LineStop] = Field(default_factory=list)


# --- /api/connections -------------------------------------------------------


class ConnectionSummary(BaseModel):
    """One trip, as listed for a line. Carries no per-stop times."""

    line_number: int
    number: int
    codes: list[int] = Field(default_factory=list)
    departureTime: str  # noqa: N815 - upstream spelling
    arrivalTime: str  # noqa: N815

    @property
    def departure(self) -> dt.time:
        return parse_hhmm(self.departureTime)

    @property
    def arrival(self) -> dt.time:
        return parse_hhmm(self.arrivalTime)


# --- /api/connectionDetail --------------------------------------------------


class ConnectionStop(BaseModel):
    number: int
    """Station number (without platform)."""
    name: str
    codes: list[int] = Field(default_factory=list)
    index: int
    """Position within the line's canonical stop list. Monotonic along a trip,
    which is what direction_id is derived from."""
    distance: int
    """Cumulative whole kilometres. Too coarse for shape_dist_traveled."""
    arrivalTime: MaybeStr = None  # noqa: N815 - only set on the final stop
    departureTime: MaybeStr = None  # noqa: N815
    platform: int

    @property
    def time(self) -> dt.time:
        """The stop's timetable time.

        Only the last stop of a trip carries ``arrivalTime``; everywhere else
        ``departureTime`` is authoritative.
        """
        raw = self.departureTime or self.arrivalTime
        if raw is None:
            raise ValueError(f"stop {self.number} ({self.name}) has no time at all")
        return parse_hhmm(raw)


class ConnectionDetail(BaseModel):
    line_number: int
    number: int
    stops: list[ConnectionStop]


# --- /api/buses -------------------------------------------------------------


class Bus(BaseModel):
    vid: str
    state_dtime: dt.datetime
    line_name: str
    line_direction: str
    destination_name: str
    last_stop_number: str
    last_stop_name: str | None = None
    current_stop_number: str
    current_stop_name: str | None = None
    current_stop_scheduled_departure: MaybeStr = None
    time_difference: MaybeStr = None
    connection_no: int
    gps_latitude: float
    gps_longitude: float
    gps_course: float | None = None

    @field_validator("state_dtime", mode="after")
    @classmethod
    def _as_utc(cls, v: dt.datetime) -> dt.datetime:
        """The upstream sends naive timestamps that are actually UTC.

        Verified against the server's own ``Date`` header: at 17:03:24 GMT the
        API reported ``state_dtime`` of ``2026-08-07 17:04:04``. Treating this
        as local time (as the previous implementation did) shifts every
        timestamp by the UTC offset.
        """
        return v.replace(tzinfo=dt.UTC) if v.tzinfo is None else v.astimezone(dt.UTC)

    # These four are ``None`` when the upstream sends something that is not a
    # number. Nothing guarantees it will not -- see :mod:`dpmp_gtfs.upstream`.

    @property
    def line(self) -> int | None:
        return whole_number(self.line_name)

    @property
    def current_station(self) -> int | None:
        """Station number decoded from ``station * 100 + platform``."""
        n = whole_number(self.current_stop_number)
        return None if n is None else n // 100

    @property
    def current_platform(self) -> int | None:
        n = whole_number(self.current_stop_number)
        return None if n is None else n % 100

    @property
    def current_stop(self) -> str | None:
        """The stop id the vehicle is approaching.

        Both consumers wanted station and platform only to spell this out, so
        it is spelled out once here instead.
        """
        station, platform = self.current_station, self.current_platform
        if station is None or platform is None:
            return None
        return stop_id(station, platform)

    @property
    def last_station(self) -> int | None:
        """Last station the vehicle passed, or ``None`` before it has departed.

        Unlike ``current_stop_number`` this is a bare station number, and the
        upstream uses ``"0"`` to mean "none yet".
        """
        return whole_number(self.last_stop_number) or None

    @property
    def countdown(self) -> dt.timedelta | None:
        """``time_difference`` parsed -- the time left until the scheduled
        departure from ``current_stop``.

        This is emphatically **not** a delay; see
        :mod:`dpmp_gtfs.realtime.tracker` for why and for what to use instead.
        """
        return parse_duration(self.time_difference) if self.time_difference else None


class BusesResponse(BaseModel):
    success: bool
    data: list[Bus] = Field(default_factory=list)
