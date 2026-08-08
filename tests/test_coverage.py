"""Tests for the GeoJSON that backs the coverage map."""

import zipfile
from pathlib import Path

from dpmp_gtfs.web.coverage import build_coverage, route_line

# --- simplification ---------------------------------------------------------

# Ramer-Douglas-Peucker itself is shapely's to get right. These assert the
# properties the map depends on, which is what would actually break if the
# dependency were swapped or its defaults changed under us.


def _coords(line: object) -> list[tuple[float, float]]:
    assert line is not None
    return [(x, y) for x, y in line.coords]  # type: ignore[attr-defined]


def test_a_straight_line_collapses_to_its_endpoints() -> None:
    line = route_line([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)], 0.001)
    assert _coords(line) == [(0.0, 0.0), (3.0, 3.0)]


def test_a_real_corner_survives() -> None:
    """Simplification must not straighten a turn the bus actually makes."""
    corner = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert _coords(route_line(corner, 0.1)) == corner


def test_endpoints_are_always_kept() -> None:
    line = [(0.0, 0.0), (0.5, 0.0001), (1.0, 0.0)]
    result = _coords(route_line(line, 10.0))
    assert result[0] == line[0]
    assert result[-1] == line[-1]


def test_a_two_point_line_passes_through_untouched() -> None:
    assert _coords(route_line([(0.0, 0.0), (1.0, 1.0)], 0.1)) == [(0.0, 0.0), (1.0, 1.0)]


def test_nothing_drawable_yields_no_geometry() -> None:
    """A single point is not a line; emitting one would be invalid GeoJSON."""
    assert route_line([(0.0, 0.0)], 0.1) is None
    assert route_line([], 0.1) is None


def test_simplification_never_reorders_points() -> None:
    line = [(0.0, 0.0), (1.0, 2.0), (2.0, 0.0), (3.0, 2.0), (4.0, 0.0)]
    result = _coords(route_line(line, 0.5))
    assert [line.index(p) for p in result] == sorted(line.index(p) for p in result)


def test_a_long_wiggle_is_actually_shortened() -> None:
    """The point of the exercise: 114k network points must not reach a browser."""
    line = [(i / 1000, (i % 3) / 100000) for i in range(500)]
    assert len(_coords(route_line(line, 0.001))) < len(line) / 5


# --- GeoJSON ----------------------------------------------------------------


def _feed(path: Path, *, with_shapes: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "L1,1,Ryba - Slovany,11\n"
            "L6,6,Vapenka - Rosice,3\n",
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,shape_id\n"
            "L1,wd,L1C1," + ("shp_a" if with_shapes else "") + "\n"
            "L6,wd,L6C1," + ("shp_b" if with_shapes else "") + "\n",
        )
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,"
            "wheelchair_boarding\n"
            "S1,Hlavni nadrazi,50.0329,15.7557,1,,1\n"
            "S1P1,Hlavni nadrazi,50.0329,15.7557,0,S1,1\n",
        )
        if with_shapes:
            zf.writestr(
                "shapes.txt",
                "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\n"
                "shp_a,50.0329,15.7557,0,0.0\n"
                "shp_a,50.0414,15.7542,1,1006.0\n"
                "shp_b,50.0329,15.7557,0,0.0\n"
                "shp_b,50.0250,15.7231,1,900.0\n",
            )
    return path


def test_routes_and_stops_become_features(tmp_path: Path) -> None:
    geo = build_coverage(_feed(tmp_path / "gtfs.zip"))

    kinds = [f["properties"]["kind"] for f in geo["features"]]
    assert kinds.count("route") == 2
    # Only parent stations are drawn; platforms would just pile up on top.
    assert kinds.count("stop") == 1


def test_trolleybus_routes_are_distinguishable(tmp_path: Path) -> None:
    geo = build_coverage(_feed(tmp_path / "gtfs.zip"))
    flags = {
        f["properties"]["route"]: f["properties"]["trolleybus"]
        for f in geo["features"]
        if f["properties"]["kind"] == "route"
    }
    assert flags == {"1": True, "6": False}


def test_geometry_uses_geojson_axis_order(tmp_path: Path) -> None:
    """GeoJSON is lon,lat -- the reverse of every other file in the feed, and
    swapping them silently puts Pardubice in the Indian Ocean."""
    geo = build_coverage(_feed(tmp_path / "gtfs.zip"))
    route = next(f for f in geo["features"] if f["properties"]["kind"] == "route")
    lon, lat = route["geometry"]["coordinates"][0]
    assert 15.0 < lon < 16.5
    assert 49.5 < lat < 50.5


def test_a_feed_without_shapes_still_yields_stops(tmp_path: Path) -> None:
    """The router can be unreachable; the map should degrade, not break."""
    geo = build_coverage(_feed(tmp_path / "gtfs.zip", with_shapes=False))
    kinds = [f["properties"]["kind"] for f in geo["features"]]
    assert kinds.count("route") == 0
    assert kinds.count("stop") == 1
