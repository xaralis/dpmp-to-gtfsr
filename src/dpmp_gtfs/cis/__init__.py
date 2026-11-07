"""The CIS JŘ timetable registry: the whole timetable, and its days of operation.

CIS is the primary source DPMP files its timetables with, and since the API
stopped answering for trips nobody is riding it is the only place the full
timetable can be read -- see ``docs/upstream-api.md``. Coordinates and realtime
stay with the API.
"""

from .archive import CisUnavailable, fetch_archives
from .timetable import Call, CisTimetable, Journey, read_timetable

__all__ = [
    "Call",
    "CisTimetable",
    "CisUnavailable",
    "Journey",
    "fetch_archives",
    "read_timetable",
]
