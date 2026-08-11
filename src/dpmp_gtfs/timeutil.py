"""Service-day arithmetic, shared by the static and realtime halves.

GTFS does not count time from midnight. It counts from the start of a *service
day*, which is why a trip leaving at 23:58 and arriving at 00:52 is written
``23:58:00`` -> ``24:52:00`` rather than wrapping to ``00:52:00``. Both halves
of this project have to agree on that convention exactly: the static feed
writes those times, and the realtime feed says which service day a vehicle's
current run belongs to. When the two disagree, consumers matching a TripUpdate
to its trip find nothing.

Keeping the convention in one module is the point. It was previously spelled
out three times with slightly different arithmetic in each.
"""

import datetime as dt
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")
"""Every scheduled time in the upstream API is local. The one exception is the
snapshot time on ``/vehicles``, which is UTC with a ``Z`` suffix."""

DAY = 24 * 3600


def format_gtfs_time(seconds: int) -> str:
    """``87780`` -> ``"24:23:00"``.

    Hours past 24 are meaningful and must not be wrapped: they are how GTFS
    expresses a trip continuing into the next calendar day.
    """
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_clock(seconds: int) -> str:
    """``87780`` -> ``"24:23"``. The same convention, for human display."""
    hours, rest = divmod(seconds, 3600)
    return f"{hours:02d}:{rest // 60:02d}"


def service_day_seconds(moment: dt.datetime, scheduled: int) -> int:
    """Seconds-from-service-day-start for ``moment``, on the day matching
    ``scheduled``.

    A vehicle observed at 00:10 local time may be 24:10 into the previous
    service day or 00:10 into the current one, and the clock alone cannot say
    which. The interpretation closest to the scheduled time wins, which is
    unambiguous as long as a vehicle is not a full twelve hours off.
    """
    local = moment.astimezone(PRAGUE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    base = int((local - midnight).total_seconds())
    return min((base - DAY, base, base + DAY), key=lambda s: abs(s - scheduled))


def service_day_date(moment: dt.datetime, scheduled: int) -> dt.date:
    """The service date ``moment`` belongs to, given a time from its schedule.

    Derived from :func:`service_day_seconds` rather than from whether the
    scheduled time exceeds 24:00, because the two are not the same question.
    A trip written ``23:58`` -> ``24:52`` has a *first departure* below 24:00,
    yet a vehicle still running it at 00:30 belongs to the previous service
    day. Testing the schedule alone gets that case wrong.
    """
    local = moment.astimezone(PRAGUE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    base = int((local - midnight).total_seconds())
    days_back = (service_day_seconds(moment, scheduled) - base) // DAY
    return (local - dt.timedelta(days=days_back)).date()
