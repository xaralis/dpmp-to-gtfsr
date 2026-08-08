from pathlib import Path

import pytest

from dpmp_gtfs.exceptions import RoutingError
from dpmp_gtfs.static.builder import apply_shapes, stop_sequences
from dpmp_gtfs.static.shapes import (
    ShapeCache,
    assemble_shape,
    build_shapes,
    decode_polyline6,
    shape_id_for,
)
from dpmp_gtfs.types import Shape, Stop, StopTime, Trip

# Real Valhalla output, recorded from a bus-costed route along line 1:
# Hlavní nádraží -> Náměstí Republiky -> Zimní stadion.
LEG_A = (
    "{nwl~A}zs`]CzHE`IAfBE|@Oz@Uh@_@\\e@Pc@He@DaA@yEIsAAc@Ha@JMFk@XaBfAuCqBg@Sk@Kg@C}HB_BLy@NkA"
    "\\yOpGuG|BsGfCmFrBeFnBaJlD_JxCu]lLybAh_@yJj@o@Hq@Nmn@|T}E|AgFjAuB`@mQlC}PjCqEM_I_@kFw@}FqA"
    "yF}AsKcDe@MaR}FsNoE}J_Du@Y}OcGeC_AgMwCsDq@oDi@mGk@cHO_E?qDVsCZsFz@{Dl@"
)
LEG_A_POINTS = 68
LEG_B = "ybhm~Asup`]}TlD_LdBi^xFiFx@kb@vG_lA`R"
LEG_B_POINTS = 7


class FakeRouter:
    """Stands in for Valhalla, recording how often it is asked."""

    def __init__(self, legs: list[str] | None = None, fail: bool = False) -> None:
        self.legs = legs or [LEG_A, LEG_B]
        self.fail = fail
        self.calls = 0

    def get_route_geometry(
        self, coordinates: list[tuple[float, float]]
    ) -> tuple[list[str], list[float]]:
        self.calls += 1
        if self.fail:
            raise RoutingError("router unreachable")
        count = len(coordinates) - 1
        return self.legs[:count], [500.0] * count


# --- ids --------------------------------------------------------------------


def test_shape_id_is_derived_from_the_stop_sequence() -> None:
    assert shape_id_for(("A", "B")) == shape_id_for(("A", "B"))
    assert shape_id_for(("A", "B")) != shape_id_for(("B", "A"))


def test_shape_id_cannot_be_confused_by_concatenation() -> None:
    """('AB','C') and ('A','BC') must not collide."""
    assert shape_id_for(("AB", "C")) != shape_id_for(("A", "BC"))


# --- polyline ---------------------------------------------------------------


def test_decode_polyline_returns_plausible_coordinates() -> None:
    points = decode_polyline6(LEG_A)
    assert len(points) == LEG_A_POINTS
    for lat, lon in points:
        assert 49.5 < lat < 50.5, "should land in the Pardubice region"
        assert 15.0 < lon < 16.5


def test_decoded_legs_join_at_their_shared_stop() -> None:
    """Leg B starts exactly where leg A ends -- the property the assembly step
    relies on to avoid duplicating a point at every stop."""
    assert decode_polyline6(LEG_A)[-1] == decode_polyline6(LEG_B)[0]


def test_decode_polyline_of_empty_string() -> None:
    assert decode_polyline6("") == []


# --- assembly ---------------------------------------------------------------


def _shape(legs: list[str], lengths: list[float]) -> Shape:
    return assemble_shape("shp_test", legs, lengths)


def test_distances_increase_along_the_shape() -> None:
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert list(shape.point_distances) == sorted(shape.point_distances)


def test_stop_distances_come_from_the_router_not_from_geometry() -> None:
    """Re-deriving them from decoded points would drift over a long route."""
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert shape.stop_distances == (0.0, 500.0, 800.0)


def test_there_is_one_stop_distance_per_stop() -> None:
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert len(shape.stop_distances) == 3  # two legs span three stops


def test_the_shared_stop_between_legs_is_not_duplicated() -> None:
    """Each leg repeats the previous leg's final point; emitting it twice
    would put a zero-length step at every stop."""
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert len(shape.points) == LEG_A_POINTS + LEG_B_POINTS - 1


def test_the_final_point_sits_exactly_at_the_final_stop() -> None:
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert shape.point_distances[-1] == shape.stop_distances[-1]


def test_every_point_has_a_distance() -> None:
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert len(shape.points) == len(shape.point_distances)


def test_a_leg_without_geometry_is_rejected() -> None:
    with pytest.raises(RoutingError, match="no usable geometry"):
        assemble_shape("shp_test", [""], [100.0])


def test_repeated_coordinates_are_collapsed() -> None:
    """The router sometimes emits the same point twice. Keeping both produces
    two shape points at one place and distance, which validators flag as
    degenerate geometry."""
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert all(a != b for a, b in zip(shape.points, shape.points[1:], strict=False))


def test_collapsing_points_does_not_disturb_stop_distances() -> None:
    """Dropping a duplicate must not lose the leg length it belonged to."""
    shape = _shape([LEG_A, LEG_B], [500.0, 300.0])
    assert shape.stop_distances == (0.0, 500.0, 800.0)


# --- cache ------------------------------------------------------------------


def test_cache_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "shapes.json"
    cache = ShapeCache(path)
    cache.put("shp_a", [LEG_A], [500.0])
    cache.save()

    assert ShapeCache(path).get("shp_a") == ([LEG_A], [500.0])


def test_corrupt_cache_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    path = tmp_path / "shapes.json"
    path.write_text("{ broken", encoding="utf8")
    assert ShapeCache(path).get("anything") is None


def test_cache_forgets_sequences_no_longer_in_use(tmp_path: Path) -> None:
    cache = ShapeCache(tmp_path / "shapes.json")
    cache.put("shp_live", [LEG_A], [1.0])
    cache.put("shp_gone", [LEG_B], [1.0])

    assert cache.prune({"shp_live"}) == 1
    assert cache.get("shp_gone") is None
    assert cache.get("shp_live") is not None


# --- build_shapes -----------------------------------------------------------


def test_cached_sequences_are_not_re_routed(tmp_path: Path) -> None:
    """The whole point of the cache: a nightly rebuild should hit the network
    zero times when routes have not changed."""
    sequences = {("A", "B", "C"): [(50.0, 15.0), (50.01, 15.01), (50.02, 15.02)]}
    cache_path = tmp_path / "shapes.json"

    first = FakeRouter()
    build_shapes(sequences, ShapeCache(cache_path), first)  # type: ignore[arg-type]
    assert first.calls == 1

    second = FakeRouter()
    shapes = build_shapes(sequences, ShapeCache(cache_path), second)  # type: ignore[arg-type]
    assert second.calls == 0
    assert len(shapes) == 1


def test_an_unreachable_router_yields_no_shapes_rather_than_failing(tmp_path: Path) -> None:
    """A feed without geometry is still a good feed."""
    sequences = {("A", "B"): [(50.0, 15.0), (50.01, 15.01)]}
    shapes = build_shapes(
        sequences,
        ShapeCache(tmp_path / "shapes.json"),
        FakeRouter(fail=True),  # type: ignore[arg-type]
    )
    assert shapes == {}


# --- wiring into the feed ---------------------------------------------------


def _stop(sid: str, lat: float, lon: float) -> Stop:
    return Stop(sid, sid, lat, lon, 0, "S1", "1", 0)


def _time(trip: str, sid: str, seq: int) -> StopTime:
    return StopTime(trip, "08:00:00", "08:00:00", sid, seq, 0, 0)


def test_trips_sharing_a_stop_sequence_share_one_shape() -> None:
    """2728 trips collapse to ~218 sequences; that ratio is what makes routing
    affordable, so it must actually hold."""
    stops = [_stop("S1P1", 50.0, 15.0), _stop("S2P1", 50.01, 15.01)]
    times = [
        _time("t1", "S1P1", 0),
        _time("t1", "S2P1", 1),
        _time("t2", "S1P1", 0),
        _time("t2", "S2P1", 1),
    ]
    assert len(stop_sequences(times, stops)) == 1


def test_opposite_directions_are_separate_sequences() -> None:
    stops = [_stop("S1P1", 50.0, 15.0), _stop("S2P1", 50.01, 15.01)]
    times = [
        _time("there", "S1P1", 0),
        _time("there", "S2P1", 1),
        _time("back", "S2P1", 0),
        _time("back", "S1P1", 1),
    ]
    assert len(stop_sequences(times, stops)) == 2


def test_apply_shapes_sets_ids_and_distances() -> None:

    shape = Shape("shp_x", ((50.0, 15.0), (50.01, 15.01)), (0.0, 800.0), (0.0, 800.0))
    trips = [Trip("L1", "wd", "t1", "x", 0, 1)]
    times = [_time("t1", "S1P1", 0), _time("t1", "S2P1", 1)]

    trips, times = apply_shapes(trips, times, {("S1P1", "S2P1"): shape})

    assert trips[0].shape_id == "shp_x"
    assert [t.shape_dist_traveled for t in times] == ["0.0", "800.0"]


def test_a_trip_without_geometry_keeps_empty_fields() -> None:
    """GTFS permits trips with no shape, so an unroutable one is left alone
    rather than dropped."""

    trips = [Trip("L1", "wd", "t1", "x", 0, 1)]
    times = [_time("t1", "S1P1", 0)]

    trips, times = apply_shapes(trips, times, {})

    assert trips[0].shape_id == ""
    assert times[0].shape_dist_traveled == ""


# --- transport --------------------------------------------------------------


def test_router_speaks_http_and_reports_failure_as_routing_error() -> None:
    """The client moved from urllib to httpx; what matters to callers is that
    a transport failure still surfaces as RoutingError, since that is what
    build_shapes catches to degrade instead of failing the build."""
    import httpx
    import respx

    from dpmp_gtfs.static.shapes import VALHALLA_URL, ValhallaRouter

    with respx.mock:
        respx.post(VALHALLA_URL).mock(side_effect=httpx.ConnectTimeout("down"))
        with pytest.raises(RoutingError, match="router unreachable"):
            ValhallaRouter(delay=0).get_route_geometry([(50.0, 15.0), (50.1, 15.1)])


def test_router_parses_a_successful_response() -> None:
    import httpx
    import respx

    from dpmp_gtfs.static.shapes import VALHALLA_URL, ValhallaRouter

    body = {"trip": {"legs": [{"shape": LEG_A, "summary": {"length": 1.006}}]}}
    with respx.mock:
        respx.post(VALHALLA_URL).mock(return_value=httpx.Response(200, json=body))
        legs, lengths = ValhallaRouter(delay=0).get_route_geometry([(50.0, 15.0), (50.1, 15.1)])

    assert legs == [LEG_A]
    assert lengths == [1006.0]


def test_a_router_error_response_is_not_mistaken_for_a_route() -> None:
    import httpx
    import respx

    from dpmp_gtfs.static.shapes import VALHALLA_URL, ValhallaRouter

    with respx.mock:
        respx.post(VALHALLA_URL).mock(return_value=httpx.Response(400, json={}))
        with pytest.raises(RoutingError):
            ValhallaRouter(delay=0).get_route_geometry([(50.0, 15.0), (50.1, 15.1)])


def test_the_politeness_delay_applies_even_when_the_router_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the pause sat on the success path only.

    This points at a community-run OSM instance. When that instance answered
    429 or 5xx -- precisely when it is under strain -- the exception path
    skipped the sleep and the next of up to 218 requests went out immediately.
    """
    import httpx

    from dpmp_gtfs.static import shapes as shapes_module

    slept: list[float] = []
    monkeypatch.setattr(shapes_module.time, "sleep", slept.append)

    def explode(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError("router is down")

    monkeypatch.setattr(shapes_module.httpx, "post", explode)

    router = shapes_module.ValhallaRouter(delay=0.25)
    with pytest.raises(RoutingError):
        router.get_route_geometry([(50.0, 15.7), (50.1, 15.8)])

    assert slept == [0.25]
