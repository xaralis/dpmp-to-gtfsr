"""Coordinates the upstream does not provide.

``/api/stations`` is incomplete: eight platforms used by real timetables have
no location. They fall into two groups, handled differently.

**Stations that exist but are missing a platform** (75/3, 149/1, 220/1). The
station itself has coordinates, so the missing platform is placed at the
station. Opposite platforms of the same stop are a few tens of metres apart,
which is well inside the tolerance of any consumer.

**Stations absent from the API entirely** (250, 252, 253). Nothing in any DPMP
or CIS dataset locates these -- notably the legacy ``STANICE.ZS`` export has
the same holes, so this is a gap in the operator's own data rather than an
artefact of the API. They are filled in from OpenStreetMap below.

Together these affect 271 of 2,728 trips across lines 1, 6, 15, 28 and 30, so
dropping them was never an option: line 6 alone would lose both terminals.

If DPMP ever completes ``/api/stations``, this table becomes dead weight --
:func:`unused_overrides` reports entries that are no longer needed.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)


class Coordinates(NamedTuple):
    latitude: float
    longitude: float
    source: str


# Sourced from OpenStreetMap (ODbL), each matched as an explicit `bus_stop`
# node and cross-checked against neighbouring stops whose positions the API
# does provide:
#
#   Vápenka        also independently confirmed to ~50 m by averaging the GPS
#                  of vehicles reporting stop 252 in /api/buses
#   Svítkov,západ  falls between Svítkov,stadion and Svítkov,park, as the
#                  route order requires
#   Mikulovice     adjacent to Mikulovice,škola, whose position is known
#
# Platforms of the same station share a position: the API gives no per-platform
# detail for these, and the two sides of a stop are a few tens of metres apart.
STATION_COORDINATES: dict[int, Coordinates] = {
    250: Coordinates(50.025557, 15.719712, "OpenStreetMap"),  # Svítkov,západ
    252: Coordinates(50.029812, 15.754485, "OpenStreetMap"),  # Vápenka
    253: Coordinates(49.990352, 15.774259, "OpenStreetMap"),  # Mikulovice
}

# Station number -> name, so a synthesised station can still be labelled. Taken
# from the stop names the timetable endpoints report for them.
STATION_NAMES: dict[int, str] = {
    250: "Svítkov,západ",
    252: "Vápenka",
    253: "Mikulovice",
}


def unused_overrides(known_stations: set[int]) -> set[int]:
    """Overrides the API has since made redundant.

    Surfaced in logs so the table does not quietly rot: once DPMP publishes
    these stations, the hand-maintained coordinates should be deleted rather
    than left to drift out of date.
    """
    return {n for n in STATION_COORDINATES if n in known_stations}
