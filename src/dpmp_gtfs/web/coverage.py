"""Network coverage as GeoJSON, for the map page.

Shapes carry 114,000 points across the network, which is right for a feed but
far too much to hand a browser. Geometry is simplified before it leaves here,
and the result is cached until the static feed changes.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Roughly ten metres at this latitude. Enough to strip redundant vertices from
# a straight road without visibly moving the line at city zoom levels.
SIMPLIFY_TOLERANCE_DEGREES = 0.0001

Point = tuple[float, float]


def simplify(points: list[Point], tolerance: float) -> list[Point]:
    """Ramer-Douglas-Peucker, iteratively to avoid recursion limits.

    Written out rather than pulled from a library: it is fifteen lines, and the
    alternative is a shapely/numpy dependency for one cosmetic step.
    """
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue

        furthest, worst = start, -1.0
        for i in range(start + 1, end):
            distance = _perpendicular_distance(points[i], points[start], points[end])
            if distance > worst:
                furthest, worst = i, distance

        if worst > tolerance:
            keep[furthest] = True
            stack.append((start, furthest))
            stack.append((furthest, end))

    return [p for p, k in zip(points, keep, strict=True) if k]


def _perpendicular_distance(point: Point, start: Point, end: Point) -> float:
    """Distance from ``point`` to the segment ``start``-``end``, in degrees."""
    (x, y), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1

    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)

    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def build_coverage(path: Path) -> dict[str, Any]:
    """Read a built feed and render routes and stops as GeoJSON.

    Shapes are grouped by the routes that use them, so the map can colour and
    label a line without the browser joining tables itself.
    """
    with zipfile.ZipFile(path) as zf:

        def rows(name: str) -> list[dict[str, str]]:
            try:
                with zf.open(name) as fh:
                    return list(csv.DictReader(io.TextIOWrapper(fh, "utf8")))
            except KeyError:
                return []

        routes = {r["route_id"]: r for r in rows("routes.txt")}
        shape_routes: dict[str, str] = {}
        for trip in rows("trips.txt"):
            if trip.get("shape_id"):
                shape_routes.setdefault(trip["shape_id"], trip["route_id"])

        points: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        for row in rows("shapes.txt"):
            points[row["shape_id"]].append(
                (
                    int(row["shape_pt_sequence"]),
                    float(row["shape_pt_lat"]),
                    float(row["shape_pt_lon"]),
                )
            )

        stops = [s for s in rows("stops.txt") if s["location_type"] == "1"]

    features: list[dict[str, Any]] = []
    raw_total = simplified_total = 0

    for shape_id, entries in points.items():
        route_id = shape_routes.get(shape_id)
        if route_id is None:
            continue
        # GeoJSON is lon,lat -- the reverse of every other file here.
        line = [(lon, lat) for _, lat, lon in sorted(entries)]
        raw_total += len(line)
        line = simplify(line, SIMPLIFY_TOLERANCE_DEGREES)
        simplified_total += len(line)

        route = routes.get(route_id, {})
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in line]},
                "properties": {
                    "kind": "route",
                    "route_id": route_id,
                    "route": route.get("route_short_name", ""),
                    "name": route.get("route_long_name", ""),
                    "trolleybus": route.get("route_type") == "11",
                },
            }
        )

    for stop in stops:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(stop["stop_lon"]), float(stop["stop_lat"])],
                },
                "properties": {
                    "kind": "stop",
                    "stop_id": stop["stop_id"],
                    "name": stop["stop_name"],
                    "step_free": stop.get("wheelchair_boarding") == "1",
                },
            }
        )

    if raw_total:
        logger.info(
            "coverage: simplified %d shape points to %d (%d%%)",
            raw_total,
            simplified_total,
            100 * simplified_total // raw_total,
        )

    return {"type": "FeatureCollection", "features": features}
