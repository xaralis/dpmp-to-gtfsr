"""What DPMP's data gets wrong, and what this project does about it.

Every correction applied to the upstream lives here. They used to be spread
across the builder, the calendar and a separate overrides module, which made
them look like ordinary implementation detail -- they are not. Each one is a
standing claim that the source data is wrong or incomplete, and each is a
liability: if DPMP fixes the underlying problem, the correction becomes a lie
that quietly outlives it.

Collected in one file they can be reviewed as a set, and the register below
says plainly what is being asserted and on what evidence.

The register
------------

**The API does not say which lines are trolleybus.** ``route_type`` has no
source in the data at all, so the split is carried as a constant, taken from
the CIS registry where trolleybus lines are published in a separate archive.

**Two code pairs are duplicates.** Codes 4 and 6 both mean Saturday; 5 and 7
both mean Sunday-and-holidays. They are per-line "fixed codes" inherited from
JDF, used only by lines 2 and 12. Left unmerged they would split identical
trips across two calendars that mean the same thing.

**Three stations are absent from /api/stations entirely** (250, 252, 253) and
**three more are missing a platform** (75/3, 149/1, 220/1). Together they
affect 271 of 2,728 trips across lines 1, 6, 15, 28 and 30 -- line 6 would
lose both terminals -- so dropping them was never an option. The legacy
``STANICE.ZS`` export has the same holes, so this is a gap in the operator's
own data rather than an artefact of the API.

**Numeric fields are strings, and nothing guarantees they hold numbers.**
``line_name`` and ``current_stop_number`` arrive as text. Parsing them with a
bare ``int()`` meant one malformed vehicle raised out of the middle of a
snapshot and discarded all fifty, freezing the realtime feed until DPMP
happened to fix it. :func:`whole_number` returns ``None`` instead, so a
vehicle the upstream describes badly is skipped and the rest are published.

Two further upstream quirks are not corrections but parsing, and stay with the
code that parses: ``state_dtime`` arrives in UTC while every scheduled time is
local (:mod:`dpmp_gtfs.api.models`), and the server answers 500 unless the
request is sent as ``text/plain`` (:mod:`dpmp_gtfs.api.client`).
"""

import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)


# --- vehicle type -----------------------------------------------------------

TROLLEYBUS_LINES = frozenset({1, 2, 3, 4, 5, 7, 11, 12, 13, 17, 27, 30, 33})
"""Lines DPMP runs as trolleybuses; everything else is a bus.

From the CIS registry, where these (655001-655033) are published in the
separate ``draha/mestske`` archive. Baked in rather than fetched: it changes at
most once every few years, and a 52 MB download to learn thirteen numbers is a
poor trade.
"""


# --- service codes ----------------------------------------------------------

# What the integers in /api/codes mean. Codes 1, 3 and 8 describe a stop or a
# vehicle; the rest describe when a trip runs.
STOP_ON_REQUEST = 1
LOW_FLOOR = 3
STEP_FREE_STOP = 8

WORKING_DAY = 2
SATURDAY = frozenset({4, 6})
SUNDAY_AND_HOLIDAYS = frozenset({5, 7})
"""Both pairs are duplicates -- see the register above. Modelled as sets so the
merge happens wherever the codes are read, not at one call site that could be
forgotten."""


# --- missing coordinates ----------------------------------------------------


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

STATION_NAMES: dict[int, str] = {
    250: "Svítkov,západ",
    252: "Vápenka",
    253: "Mikulovice",
}
"""Names for the synthesised stations, taken from what the timetable endpoints
call them."""


def unused_overrides(known_stations: set[int]) -> set[int]:
    """Corrections the API has since made redundant.

    Surfaced in logs so the table does not quietly rot: once DPMP publishes
    these stations, the hand-maintained coordinates should be deleted rather
    than left to drift out of date behind the real ones.
    """
    return {n for n in STATION_COORDINATES if n in known_stations}


# --- malformed fields -------------------------------------------------------


def whole_number(value: str | None) -> int | None:
    """An upstream integer field, or ``None`` when it does not hold one.

    Tolerant on purpose. The alternative -- letting ``int()`` raise -- costs
    the entire snapshot rather than the one vehicle the upstream described
    badly, and the realtime feed then stays frozen on the last good snapshot
    for as long as the bad record persists. A missing vehicle is a far smaller
    lie than fifty stale ones.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
