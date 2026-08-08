"""Road geometry for trips, routed against OpenStreetMap.

The API gives stop coordinates but no route geometry, so without this every
consumer draws vehicles cutting straight across blocks and rivers. Valhalla
turns each stop sequence into a line that follows real streets.

Two things make this cheap enough to run on every rebuild:

* **Trips share geometry.** 2,728 trips collapse into 218 distinct stop
  sequences, so that is 218 routing calls rather than one per trip.
* **Results are cached by stop sequence.** Timetables change far more often
  than routes do, so a normal nightly rebuild issues no requests at all.

Valhalla's ``bus`` costing is used rather than plain driving: it honours
bus-only lanes and turn restrictions that apply to service vehicles. It is
also the closest available model for trolleybuses, which have no costing of
their own.

Geometry is never required. If the router is unreachable the feed is built
without ``shapes.txt`` -- degraded, but valid and still useful.
"""

import hashlib
import json
import logging
import math
import time
from itertools import pairwise
from pathlib import Path

import httpx

from dpmp_gtfs.exceptions import RoutingError
from dpmp_gtfs.types import Point, Shape, StopSequence

logger = logging.getLogger(__name__)

VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
EARTH_RADIUS_M = 6371000.0

# Valhalla caps a single request's locations; no Pardubice route comes close
# (the longest calls at 48 stops), but the guard keeps a future change honest.
MAX_LOCATIONS = 100


def shape_id_for(sequence: StopSequence) -> str:
    """A stable id derived from the stop sequence itself.

    Deriving it from content rather than position means adding a line does not
    renumber every other shape, so ids stay comparable between feed versions.
    """
    digest = hashlib.sha256("\x00".join(sequence).encode()).hexdigest()
    return f"shp_{digest[:12]}"


def decode_polyline6(encoded: str) -> list[Point]:
    """Decode Valhalla's polyline format (six decimal places)."""
    coords: list[Point] = []
    index = lat = lon = 0

    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coords.append((lat / 1e6, lon / 1e6))

    return coords


class ValhallaRouter:
    """Minimal Valhalla client.

    Deliberately polite: this points at a community-run OSM instance by
    default, so requests are serialised with a delay and only ever issued for
    sequences missing from the cache.
    """

    def __init__(
        self,
        url: str = VALHALLA_URL,
        user_agent: str = "dpmp-to-gtfsr/0.1 (+https://github.com/xaralis/dpmp-to-gtfsr)",
        timeout: float = 60.0,
        delay: float = 1.0,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout
        self.delay = delay

    def get_route_geometry(self, coordinates: list[Point]) -> tuple[list[str], list[float]]:
        """Route through every coordinate in order.

        Returns each leg's encoded geometry and its length in metres. Legs map
        one-to-one onto the gaps between stops, which is what makes exact
        per-stop distances possible.
        """
        if len(coordinates) < 2:
            raise RoutingError("a shape needs at least two stops")
        if len(coordinates) > MAX_LOCATIONS:
            raise RoutingError(f"{len(coordinates)} stops exceeds the router's limit")

        payload = {
            "locations": [{"lat": lat, "lon": lon, "type": "break"} for lat, lon in coordinates],
            "costing": "bus",
            "directions_options": {"units": "kilometers"},
        }

        try:
            response = httpx.post(
                self.url,
                json=payload,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise RoutingError(f"router unreachable: {exc!r}") from exc

        legs = body.get("trip", {}).get("legs")
        if not legs:
            raise RoutingError(f"router returned no route: {body.get('trip', {}).get('status')}")
        if len(legs) != len(coordinates) - 1:
            raise RoutingError(f"expected {len(coordinates) - 1} legs, got {len(legs)}")

        if self.delay:
            time.sleep(self.delay)

        return (
            [leg["shape"] for leg in legs],
            [leg["summary"]["length"] * 1000.0 for leg in legs],
        )


class ShapeCache:
    """Routed geometry, keyed by stop sequence.

    Stored as the router's own encoded polylines, which keeps the file about
    an order of magnitude smaller than expanded coordinates.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, list[str] | list[float]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._entries = json.loads(self.path.read_text(encoding="utf8"))
            logger.info("loaded %d cached shapes", len(self._entries))
        except json.JSONDecodeError, OSError:
            # A corrupt cache costs routing time, never correctness.
            logger.warning("could not read shape cache at %s, starting empty", self.path)
            self._entries = {}

    def get(self, key: str) -> tuple[list[str], list[float]] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        return list(entry["legs"]), list(entry["lengths"])  # type: ignore[arg-type]

    def put(self, key: str, legs: list[str], lengths: list[float]) -> None:
        self._entries[key] = {"legs": legs, "lengths": lengths}

    def prune(self, live_keys: set[str]) -> int:
        """Forget sequences no timetable uses any more."""
        stale = set(self._entries) - live_keys
        for key in stale:
            del self._entries[key]
        return len(stale)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, sort_keys=True), encoding="utf8")


def _step_lengths(points: list[Point]) -> list[float]:
    """Straight-line distance between consecutive points, in metres.

    An equirectangular approximation is plenty at city scale, and it avoids a
    geodesy dependency for something only used to spread a leg's length across
    its own points.
    """
    steps = []

    for (lat1, lon1), (lat2, lon2) in pairwise(points):
        mean_lat = math.radians((lat1 + lat2) / 2)
        dx = math.radians(lon2 - lon1) * math.cos(mean_lat)
        dy = math.radians(lat2 - lat1)
        steps.append(math.hypot(dx, dy) * EARTH_RADIUS_M)

    return steps


def assemble_shape(shape_id: str, legs: list[str], lengths: list[float]) -> Shape:
    """Stitch routed legs into one shape with cumulative distances.

    Each leg spans one gap between stops, so stop distances are just the
    running total of leg lengths -- taken from the router, not re-derived, so
    they cannot drift over a long route.

    Within a leg the router reports only a total, so its length is spread
    across its points in proportion to straight-line spacing. Over a few
    hundred metres of road that is well inside the precision anything uses
    ``shape_dist_traveled`` for.
    """
    points: list[Point] = []
    point_distances: list[float] = []
    stop_distances: list[float] = [0.0]

    for index, (encoded, leg_length) in enumerate(zip(legs, lengths, strict=True)):
        leg_points = decode_polyline6(encoded)
        if len(leg_points) < 2:
            raise RoutingError(f"leg {index} of {shape_id} has no usable geometry")

        start_distance = stop_distances[-1]
        steps = _step_lengths(leg_points)
        total_step = sum(steps)

        # The first point of every leg after the first repeats the previous
        # leg's last point; emitting it again would put a zero-length step at
        # every stop.
        if index == 0:
            points.append(leg_points[0])
            point_distances.append(start_distance)

        travelled = 0.0
        for point, step in zip(leg_points[1:], steps, strict=True):
            travelled += step
            # The router occasionally repeats a coordinate. Keeping it would
            # emit two shape points at the same place and distance, which
            # consumers flag as degenerate geometry.
            if points and point == points[-1]:
                continue
            fraction = travelled / total_step if total_step else 1.0
            points.append(point)
            point_distances.append(start_distance + leg_length * fraction)

        # Pin the leg's end exactly to the router's figure. A leg whose points
        # all collapsed into the previous one leaves nothing to pin, but its
        # length still counts towards the next stop.
        end_distance = start_distance + leg_length
        if point_distances:
            point_distances[-1] = end_distance
        stop_distances.append(end_distance)

    return Shape(
        shape_id=shape_id,
        points=tuple(points),
        point_distances=tuple(point_distances),
        stop_distances=tuple(stop_distances),
    )


def build_shapes(
    sequences: dict[StopSequence, list[Point]],
    cache: ShapeCache,
    router: ValhallaRouter | None = None,
) -> dict[StopSequence, Shape]:
    """Route every stop sequence, reusing cached geometry where possible.

    Sequences that cannot be routed are simply left out; their trips end up
    without a ``shape_id``, which GTFS permits.
    """
    router = router or ValhallaRouter()
    shapes: dict[StopSequence, Shape] = {}
    keys = {seq: shape_id_for(seq) for seq in sequences}

    if missing := [seq for seq in sequences if cache.get(keys[seq]) is None]:
        logger.info(
            "routing %d new shapes (%d cached)", len(missing), len(sequences) - len(missing)
        )

    failures = 0

    for sequence, coordinates in sequences.items():
        key = keys[sequence]
        cached = cache.get(key)

        if cached is None:
            try:
                legs, lengths = router.get_route_geometry(coordinates)
            except RoutingError as exc:
                failures += 1
                logger.warning("could not route shape %s: %s", key, exc)
                continue
            cache.put(key, legs, lengths)
        else:
            legs, lengths = cached

        try:
            shapes[sequence] = assemble_shape(key, legs, lengths)
        except (RoutingError, ValueError) as exc:
            failures += 1
            logger.warning("could not assemble shape %s: %s", key, exc)

    if pruned := cache.prune(set(keys.values())):
        logger.info("dropped %d shapes no longer used by any trip", pruned)

    cache.save()

    if failures:
        logger.warning("%d of %d shapes unavailable", failures, len(sequences))

    logger.info("built %d shapes covering %d stop sequences", len(shapes), len(sequences))
    return shapes
