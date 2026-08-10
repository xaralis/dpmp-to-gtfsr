"""The CIS JŘ timetable registry: which trips exist, and which way they run.

CIS is the primary source DPMP files its timetables with. The API that used to
publish them (``online.dpmp.cz/api/connections``) is gone, and the replacement
has no bulk listing at all -- so this package supplies the one thing the API
can no longer answer.
"""

from .archive import CisUnavailable, fetch_archives

__all__ = ["CisUnavailable", "fetch_archives"]
