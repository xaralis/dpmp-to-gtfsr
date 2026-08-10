"""Splits a line's trips into its two directions.

The API states no direction anywhere: a trip is a line id, a trip number, its
fixed codes and its stops. Their own app does not need one either -- it shows a
destination, not a direction. ``direction_id`` is a GTFS construct, and GTFS
only asks that the two directions be told apart consistently; which one is 0
carries no meaning.

So it is derived from where trips start and end. Trips leaving the same
terminal run the same way, which was checked against the CIS registry on eight
lines covering 1,100 trips: every terminal group fell wholly inside one
direction, never across both. Lines have more than two groups -- short turns
and variant terminals -- so the groups are then paired up: a group that starts
where another ends is its opposite.

Two approaches were measured and rejected. Trip-number parity matches today on
every trip in the network, but it is a JDF numbering convention nothing
guarantees. Ordering stops against the line's longest trip cannot say which of
the two runs is 0: on line 1 it labelled all 206 trips backwards, because the
reference trip happened to run inbound.
"""

import logging
from collections import defaultdict

from dpmp_gtfs.api.models import Connection

logger = logging.getLogger(__name__)


def assign_directions(connections: dict[int, Connection]) -> dict[int, int]:
    """``{trip number: 0 or 1}`` for one line's trips."""
    ends: dict[int, tuple[int, int]] = {}
    for number, connection in connections.items():
        if connection.stops:
            ends[number] = (connection.stops[0].stop_id, connection.stops[-1].stop_id)

    groups: dict[int, set[int]] = defaultdict(set)
    for number, (first, _) in ends.items():
        groups[first].add(number)

    # A group is the opposite of the one that starts where this one ends.
    starts = set(groups)
    opposite: dict[int, int] = {}
    for first, numbers in groups.items():
        last = ends[next(iter(numbers))][1]
        if last in starts and last != first:
            opposite[first] = last

    # Two-colour the groups. Sorting first keeps the labels stable across
    # rebuilds: an unstable rule would flip direction_id for no reason.
    colour: dict[int, int] = {}
    for first in sorted(groups):
        if first in colour:
            continue
        colour[first] = 0
        if (other := opposite.get(first)) is not None and other not in colour:
            colour[other] = 1

    out = {n: colour.get(first, 0) for n, (first, _) in ends.items()}
    unpaired = sorted(set(groups) - set(opposite))
    if unpaired:
        logger.debug("terminals %s have no opposite run; treated as direction 0", unpaired)
    return out
