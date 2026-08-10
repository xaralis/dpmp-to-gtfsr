"""The CIS JŘ timetable registry, read for one thing only: days of operation.

CIS is the primary source DPMP files its timetables with, and its calendars
agree with the published paper timetable where the API's ``fixedCodes`` do not
-- see ``docs/upstream-api.md``. Nothing else crosses this boundary: which
trips exist, their times, stops, directions and realtime all stay with the API.
"""

from .archive import CisUnavailable, fetch_archives
from .calendars import build_calendars

__all__ = ["CisUnavailable", "build_calendars", "fetch_archives"]
