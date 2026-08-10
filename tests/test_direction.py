import random

from dpmp_gtfs.api.models import Connection
from dpmp_gtfs.static.direction import assign_directions


def _conn(number: int, stops: list[int]) -> Connection:
    return Connection.model_validate({
        "lineId": "1", "connectionId": number, "fixedCodes": ["X"],
        "stops": [{"stopId": s, "platformId": "1", "departureTime": "04:00:00"} for s in stops],
    })


def test_opposite_runs_get_opposite_ids():
    out = assign_directions({1: _conn(1, [10, 20, 30]), 2: _conn(2, [30, 20, 10])})
    assert out[1] != out[2]
    assert set(out.values()) == {0, 1}


def test_trips_sharing_a_terminal_share_a_direction():
    # A short turn (trip 3) shares two stops with the full trip (1) in the
    # same relative order -- that is enough evidence on its own, no terminal
    # grouping needed.
    out = assign_directions({
        1: _conn(1, [10, 20, 30]),
        3: _conn(3, [10, 20]),        # a short turn, same way
        2: _conn(2, [30, 20, 10]),
    })
    assert out[1] == out[3] != out[2]


def test_the_label_is_stable_not_arbitrary():
    # Same input in a different order must produce the same labels, or a
    # rebuild would flip direction_id for no reason.
    a = assign_directions({1: _conn(1, [10, 30]), 2: _conn(2, [30, 10])})
    b = assign_directions({2: _conn(2, [30, 10]), 1: _conn(1, [10, 30])})
    assert a == b


def test_a_single_direction_line_is_all_zero():
    out = assign_directions({1: _conn(1, [10, 20]), 3: _conn(3, [10, 20])})
    assert set(out.values()) == {0}


def test_trips_sharing_fewer_than_two_stops_land_in_separate_components():
    # No stops in common at all.
    out = assign_directions({1: _conn(1, [10, 20]), 2: _conn(2, [30, 40])})
    assert set(out) == {1, 2}
    assert out[1] == 0
    assert out[2] == 0

    # Exactly one stop in common -- still not enough to compare an order.
    out = assign_directions({1: _conn(1, [10, 20]), 2: _conn(2, [20, 30])})
    assert set(out) == {1, 2}
    assert out[1] == 0
    assert out[2] == 0


def test_heterogeneous_group_is_stable_across_orderings():
    # The adversarial case from the previous review round: a first-stop
    # terminal (10) shared by a full trip (1) and a short turn (9), plus
    # their return runs (2 and 5). Reordering the input dict must not change
    # the result.
    trips = {
        1: _conn(1, [10, 20, 30]),
        2: _conn(2, [30, 20, 10]),
        5: _conn(5, [20, 10]),
        9: _conn(9, [10, 20]),
    }
    reference = assign_directions(dict(trips))
    keys = list(trips)
    for _ in range(20):
        random.shuffle(keys)
        out = assign_directions({key: trips[key] for key in keys})
        assert out == reference


def test_a_pure_loop_line_does_not_crash_and_is_stable():
    # Dubina,centrum -> Svítkov,škola -> Dubina,centrum: first stop equals
    # last stop. One trip repeats the same loop, one runs it the other way,
    # so the order comparison has to cope with a stop appearing twice.
    trips = {
        1: _conn(1, [50, 60, 70, 50]),
        2: _conn(2, [50, 70, 60, 50]),
        3: _conn(3, [50, 60, 70, 50]),
    }
    reference = assign_directions(dict(trips))
    assert set(reference) == {1, 2, 3}
    keys = list(trips)
    for _ in range(20):
        random.shuffle(keys)
        out = assign_directions({key: trips[key] for key in keys})
        assert out == reference
