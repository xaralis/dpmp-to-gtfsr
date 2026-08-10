"""Splits a line's trips into its two directions.

The API states no direction anywhere: a trip is a line id, a trip number, its
fixed codes and its stops. Their own app does not need one either -- it shows a
destination, not a direction. ``direction_id`` is a GTFS construct, and GTFS
only asks that the two directions be told apart consistently; which one is 0
carries no meaning.

Two approaches were measured and rejected before this one. Trip-number parity
matches today on every trip in the network, but it is a JDF numbering
convention nothing guarantees. Ordering stops against the line's longest trip
cannot say which of the two runs is 0: on line 1 it labelled all 206 trips
backwards, because the reference trip happened to run inbound. A third
approach -- grouping trips by their first stop, then pairing groups that start
where another ends -- worked on measured data but was a heuristic standing in
for something exact: a group could genuinely contain more than one distinct
last stop (a full trip and a short turn sharing an origin), so it needed a
"representative" last stop for the group, and picking one non-deterministically
made ``direction_id`` flip across otherwise-identical rebuilds. Voting by the
most common last stop made that specific flip deterministic, but it was still
a patch on a heuristic that should not have needed one.

What replaced all three: direction can be read off the stop *order* itself, no
representative required. Two trips that share at least two stops either visit
them in the same relative order (same direction) or the reversed order
(opposite direction) -- there is no third option unless the comparison is
ambiguous (see below). That gives an exact same/opposite relation between
every pair of trips that share enough stops to compare: no representative, no
majority vote, no terminals. The relation is used as an edge in a graph over
the line's trips -- same-direction edges and opposite-direction edges -- the
graph is two-coloured by traversal, and each connected component gets its own
colouring, seeded independently at 0 for whichever trip number in it sorts
lowest. Trips are always visited in sorted order, so which node seeds which
component -- and therefore the whole split -- can never depend on the order
trips were handed in.

Measured across 852 trips on six lines, the relation itself is exact: zero
conflicting edges anywhere, and on five of the six lines the graph is a single
connected component, meaning the split is forced rather than chosen::

    line  trips  components  conflicts  agrees with CIS
    1       206           1          0        206/206
    5       213           1          0        213/213
    2       211           1          0        211/211
    12       88           1          0          88/88
    88       14           2          0          14/14
    8       120           1          0         96/120

The one genuine ambiguity is loop lines, and it is real, not a defect in the
method. Line 88 (``Dubina,centrum -> Svítkov,škola -> Dubina,centrum``) splits
into two components because some of its trips share fewer than two stops with
the rest, so the relative labelling *between* those components is genuinely
undetermined -- there is no shared-stop evidence to decide it either way, and
guessing would be exactly the kind of unfounded call this approach exists to
avoid. Line 8 disagrees with CIS on 24 of its 120 trips for the same reason: on
a loop there is no geometric "outbound", and CIS's ``_out``/``_in`` reflects
its JDF numbering pattern, not the shape of the route. Neither source is
wrong; the question has no geometric answer there.
"""

import itertools
import logging
from collections import defaultdict

from dpmp_gtfs.api.models import Connection

logger = logging.getLogger(__name__)


def _first_occurrences(stop_ids: list[int]) -> dict[int, int]:
    """Each stop's first position in the sequence.

    A trip can revisit a stop (a loop), so only the first occurrence is kept
    -- consistent index bookkeeping beats trying to disambiguate repeats.
    """
    order: dict[int, int] = {}
    for index, stop_id in enumerate(stop_ids):
        order.setdefault(stop_id, index)
    return order


def _relation(order_a: dict[int, int], order_b: dict[int, int]) -> int | None:
    """``1`` same direction, ``-1`` opposite, ``None`` no verdict.

    No verdict covers two cases: fewer than two shared stops to compare (no
    evidence either way), and a genuine mix of agreeing and disagreeing pairs
    among the shared stops (possible when a trip revisits a stop) -- treated
    as no edge rather than forcing a call either approach could get wrong.
    """
    shared = sorted(set(order_a) & set(order_b))
    if len(shared) < 2:
        return None

    agree = False
    disagree = False
    for stop_a, stop_b in itertools.combinations(shared, 2):
        same_in_a = order_a[stop_a] < order_a[stop_b]
        same_in_b = order_b[stop_a] < order_b[stop_b]
        if same_in_a == same_in_b:
            agree = True
        else:
            disagree = True

    if agree and not disagree:
        return 1
    if disagree and not agree:
        return -1
    return None


def assign_directions(connections: dict[int, Connection]) -> dict[int, int]:
    """``{trip number: 0 or 1}`` for one line's trips."""
    numbers = sorted(connections)
    orders = {n: _first_occurrences([s.stop_id for s in connections[n].stops]) for n in numbers}
    line_id = connections[numbers[0]].line_id if numbers else ""

    same: dict[int, set[int]] = defaultdict(set)
    opposite: dict[int, set[int]] = defaultdict(set)
    for a, b in itertools.combinations(numbers, 2):
        relation = _relation(orders[a], orders[b])
        if relation == 1:
            same[a].add(b)
            same[b].add(a)
        elif relation == -1:
            opposite[a].add(b)
            opposite[b].add(a)

    # Two-colour the graph, one connected component at a time. Components are
    # seeded in sorted trip-number order, so the seed -- and everything
    # propagated from it -- never depends on the input dict's order.
    colour: dict[int, int] = {}
    for seed in numbers:
        if seed in colour:
            continue
        colour[seed] = 0
        stack = [seed]
        while stack:
            node = stack.pop()
            for neighbour in same[node]:
                _propagate(colour, stack, line_id, node, neighbour, colour[node])
            for neighbour in opposite[node]:
                _propagate(colour, stack, line_id, node, neighbour, 1 - colour[node])

    return colour


def _propagate(
    colour: dict[int, int], stack: list[int], line_id: str, node: int, neighbour: int, expected: int
) -> None:
    """Assign ``neighbour`` its colour, or flag a contradiction.

    A contradiction would mean the same/opposite relation formed a cycle that
    does not close consistently -- measured to never happen, so this is a
    smoke alarm, not a code path relied on to pick a side.
    """
    if neighbour in colour:
        if colour[neighbour] != expected:
            logger.warning(
                "line %s: trips %d and %d contradict the direction already assigned",
                line_id,
                node,
                neighbour,
            )
        return
    colour[neighbour] = expected
    stack.append(neighbour)
