"""Typed models for the api.mhdonline.cz responses.

Everything the upstream does oddly is normalised here, once:

* ``currentDelay`` is an ISO-8601 duration string and is a **real** delay --
  unlike the old API's ``time_difference``, which counted down to the next
  scheduled departure and was not one.
* ``fixedCodes`` appear on both trips and stops, and the same letter means
  different things at each level. Case matters: ``X`` on a trip is "runs on
  weekdays", ``x`` on a stop is "request stop".
* ``lineId`` is a string, and ``jdfId`` is the line's JDF number (e.g.
  ``655001``) -- kept only as an identifier, nothing in this project joins
  against it any more.
* A connection's final stop carries only ``arrivalTime`` -- there is nowhere
  left to depart to. ``ConnectionStop.departure`` falls back to it, the same
  way the old API's ``ConnectionStop.time`` did.
"""

import datetime as dt
import logging
import re

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DURATION = re.compile(
    r"^(?P<sign>-?)P(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)
_HHMMSS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")

# Trip-level fixed codes, from the JDF 1.10 table of fixed codes (Ministerstvo
# dopravy, Odbor veřejné dopravy). The old API published these meanings at
# ``/api/codes``; the new one does not, so they are spelled out here.
WORKING_DAYS = "X"
SATURDAY = "6"
SUNDAY_AND_HOLIDAYS = "+"
"""``+``: "jede v neděli a ve státem uznané svátky"."""
SUNDAY = "7"
""""jede v neděli" -- and, unlike ``+``, *not* on state holidays.

The distinction is real, not pedantic: it is what keeps a trip marked ``7`` off
the road on Christmas Day.
"""
PER_WEEKDAY = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4, SATURDAY: 5, SUNDAY: 6}
"""Codes naming a single weekday, as Python's ``date.weekday()`` numbering.

Used by the airport shuttle (line 90), which runs on the days flights leave
rather than on any weekday/weekend pattern.
"""
LOW_FLOOR = "@"

# Stop-level fixed codes. Note the case clash with WORKING_DAYS above.
STEP_FREE_STOP = "@"
STOP_ON_REQUEST = "x"


def parse_iso_duration(value: str) -> dt.timedelta:
    """``"-PT1M43S"`` -> ``timedelta(seconds=-103)``.

    The sign applies to the whole magnitude, so a vehicle 103 seconds *early*
    reads as a negative delay -- which is exactly what GTFS-RT wants.
    """
    m = _DURATION.match(value)
    if not m or value in ("P", "PT", "-P", "-PT"):
        raise ValueError(f"not an ISO-8601 duration: {value!r}")
    magnitude = dt.timedelta(
        days=int(m.group("d") or 0),
        hours=int(m.group("h") or 0),
        minutes=int(m.group("m") or 0),
        seconds=float(m.group("s") or 0),
    )
    return -magnitude if m.group("sign") else magnitude


def parse_hhmmss(value: str) -> dt.time:
    """``"04:12:00"`` -> ``04:12:00``."""
    m = _HHMMSS.match(value)
    if not m:
        raise ValueError(f"not a HH:MM:SS time: {value!r}")
    return dt.time(int(m.group(1)), int(m.group(2)), int(m.group(3)))


# --- /{provider}/vehicles ---------------------------------------------------


class Vehicle(BaseModel):
    vid: str
    line_id: str = Field(alias="lineId")
    line_direction: str = Field(alias="lineDirection", default="")
    destination_name: str = Field(alias="destinationName", default="")
    last_stop_id: int | None = Field(alias="lastStopId", default=None)
    next_stop_id: int | None = Field(alias="nextStopId", default=None)
    next_stop_platform_id: int | None = Field(alias="nextStopPlatformId", default=None)
    next_stop_scheduled_departure: str | None = Field(
        alias="nextStopScheduledDepartureTime", default=None
    )
    gps_latitude: float = Field(alias="gpsLat")
    gps_longitude: float = Field(alias="gpsLon")
    current_delay: str | None = Field(alias="currentDelay", default=None)
    connection_id: int = Field(alias="connectionId")
    on_station: bool = Field(alias="onStation", default=False)

    model_config = {"populate_by_name": True}

    @property
    def delay(self) -> dt.timedelta | None:
        """The vehicle's delay, or ``None`` when the upstream reports none.

        Absent is not zero: publishing zero would assert punctuality for every
        vehicle the upstream declined to describe -- that distinction is the
        whole reason this returns ``None`` rather than ``timedelta(0)`` for a
        missing value, and it stays load-bearing here.

        ``currentDelay`` is typed as a plain ``str``, so a value that is
        *present* but not a valid ISO-8601 duration reaches here too. That is
        a different case from absence: the upstream did try to describe this
        vehicle, it just did so badly, so this is treated as zero delay
        (logged as a warning) rather than as "no evidence either way" --
        a deliberate product decision, not the more cautious ``None`` the
        absent/null case gets. Having the trip present with no delay beats
        having it disappear from the feed entirely.
        """
        if not self.current_delay:
            return None
        try:
            return parse_iso_duration(self.current_delay)
        except ValueError:
            logger.warning(
                "vehicle %s has an unparseable currentDelay: %r -- treating as zero delay",
                self.vid,
                self.current_delay,
            )
            return dt.timedelta(0)


class VehiclesResponse(BaseModel):
    time: dt.datetime
    vehicles: list[Vehicle] = Field(default_factory=list)


# --- /{provider}/stops and /{provider}/lines --------------------------------


class Stop(BaseModel):
    id: int
    name: str
    gps_latitude: float | None = Field(alias="gpsLat", default=None)
    gps_longitude: float | None = Field(alias="gpsLon", default=None)
    """Absent for a handful of stops (e.g. 147, "Opočínek,rozvodna") -- ``None``
    rather than a required field, so one such record does not fail validation
    for the whole ``/stops`` payload. :func:`dpmp_gtfs.static.builder.build_stops`
    is what actually drops these; this model only has to admit the possibility."""
    fixed_codes: list[str] = Field(alias="fixedCodes", default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def step_free(self) -> bool:
        return STEP_FREE_STOP in self.fixed_codes

    @property
    def on_request(self) -> bool:
        """"Zastávka na znamení" -- the vehicle only calls if asked."""
        return STOP_ON_REQUEST in self.fixed_codes


class Line(BaseModel):
    id: str
    jdf_id: str = Field(alias="jdfId")
    """The line's JDF number, e.g. ``655001``. Just an identifier."""
    enabled: bool = True

    model_config = {"populate_by_name": True}


# --- /{provider}/connections/{line}/{number} --------------------------------


class ConnectionStop(BaseModel):
    stop_id: int = Field(alias="stopId")
    platform_id: str = Field(alias="platformId", default="")
    departure_time: str | None = Field(alias="departureTime", default=None)
    arrival_time: str | None = Field(alias="arrivalTime", default=None)

    model_config = {"populate_by_name": True}

    @property
    def departure(self) -> dt.time:
        """The stop's timetable time.

        Only the trip's final stop omits ``departureTime`` -- there is
        nowhere left to depart to -- and carries ``arrivalTime`` instead.
        """
        raw = self.departure_time or self.arrival_time
        if raw is None:
            raise ValueError(f"stop {self.stop_id} has no time at all")
        return parse_hhmmss(raw)


class Connection(BaseModel):
    line_id: str = Field(alias="lineId")
    connection_id: int = Field(alias="connectionId")
    fixed_codes: list[str] = Field(alias="fixedCodes", default_factory=list)
    stops: list[ConnectionStop] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def low_floor(self) -> bool:
        return LOW_FLOOR in self.fixed_codes
