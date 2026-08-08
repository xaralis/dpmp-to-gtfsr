"""Network coverage as GeoJSON, for the map page.

Shapes carry 114,000 points across the network, which is right for a feed but
far too much to hand a browser. Geometry is simplified before it leaves here,
and the result is cached until the static feed changes.
"""

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from shapely.geometry import LineString, Point, mapping

from dpmp_gtfs.archive import read_tables

logger = logging.getLogger(__name__)

# Roughly ten metres at this latitude. Enough to strip redundant vertices from
# a straight road without visibly moving the line at city zoom levels.
SIMPLIFY_TOLERANCE_DEGREES = 0.0001

LonLat = tuple[float, float]
"""A position, longitude first -- what GeoJSON requires, and the reverse of
:data:`dpmp_gtfs.types.LatLon` used everywhere else. Both names say their order
because nothing else will: they are the same type, and getting it wrong fails
silently."""


def route_line(coordinates: list[LonLat], tolerance: float) -> LineString | None:
    """One route's drawable geometry, or ``None`` when there is nothing to draw.

    Simplification is Ramer-Douglas-Peucker, left to shapely. It was hand-written
    here until it was measured: shapely gives byte-identical output on all 218
    shapes (11,232 points from 114,079) in a sixth of the time, and RDP has
    well-known edge cases around collinear and duplicate points that this project
    has no reason to be maintaining an implementation of.

    ``preserve_topology=False`` selects plain RDP. The topology-preserving
    variant spends time refusing to simplify a line into self-intersection,
    which does not matter for drawing a bus route.
    """
    if len(coordinates) < 2:
        # A single point is not a line, and GeoJSON would reject it.
        return None
    # simplify() is declared as returning any geometry; simplifying a LineString
    # only ever yields a LineString.
    return cast(LineString, LineString(coordinates).simplify(tolerance, preserve_topology=False))


def build_coverage(path: Path) -> dict[str, Any]:
    """Read a built feed and render routes and stops as GeoJSON.

    Shapes are grouped by the routes that use them, so the map can colour and
    label a line without the browser joining tables itself.
    """
    tables = read_tables(
        path, "routes.txt", "trips.txt", "shapes.txt", "stops.txt", "stop_times.txt"
    )

    routes = {r["route_id"]: r for r in tables["routes.txt"]}

    # One pass over trips.txt answers both questions asked of it: which route
    # drew each shape, and which route each trip belongs to.
    shape_routes: dict[str, str] = {}
    trip_routes: dict[str, str] = {}
    for trip in tables["trips.txt"]:
        trip_routes[trip["trip_id"]] = trip["route_id"]
        if trip.get("shape_id"):
            shape_routes.setdefault(trip["shape_id"], trip["route_id"])

    points: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in tables["shapes.txt"]:
        points[row["shape_id"]].append(
            (
                int(row["shape_pt_sequence"]),
                float(row["shape_pt_lat"]),
                float(row["shape_pt_lon"]),
            )
        )

    all_stops = tables["stops.txt"]
    stops = [s for s in all_stops if s["location_type"] == "1"]
    platforms = {s["stop_id"]: s for s in all_stops if s["location_type"] == "0"}

    # Which lines call at each platform. Emitted once per platform with a list
    # of lines, not once per line: interchanges are served by a dozen routes
    # each, and repeating them tripled the payload for nothing.
    platform_routes: dict[str, set[str]] = {}
    for row in tables["stop_times.txt"]:
        route_id = trip_routes.get(row["trip_id"])
        if route_id:
            platform_routes.setdefault(row["stop_id"], set()).add(route_id)

    features: list[dict[str, Any]] = []
    raw_total = simplified_total = 0

    for shape_id, entries in points.items():
        route_id = shape_routes.get(shape_id)
        if route_id is None:
            continue

        line = route_line(
            [(lon, lat) for _, lat, lon in sorted(entries)], SIMPLIFY_TOLERANCE_DEGREES
        )
        if line is None:
            continue
        raw_total += len(entries)
        simplified_total += len(line.coords)

        route = routes.get(route_id, {})
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(line),
                "properties": {
                    "kind": "route",
                    "route_id": route_id,
                    "route": route.get("route_short_name", ""),
                    "name": route.get("route_long_name", ""),
                    "trolleybus": route.get("route_type") == "11",
                },
            }
        )

    for stop_ref, serving in sorted(platform_routes.items()):
        platform = platforms.get(stop_ref)
        if platform is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(
                    Point(float(platform["stop_lon"]), float(platform["stop_lat"]))
                ),
                "properties": {
                    "kind": "platform",
                    "stop_id": stop_ref,
                    "name": platform["stop_name"],
                    "platform": platform.get("platform_code", ""),
                    "lines": sorted(
                        (routes.get(r, {}).get("route_short_name", "") for r in serving),
                        key=lambda n: (len(n), n),
                    ),
                },
            }
        )

    for stop in stops:
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(Point(float(stop["stop_lon"]), float(stop["stop_lat"]))),
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
