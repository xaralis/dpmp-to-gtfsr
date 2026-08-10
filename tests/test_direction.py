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
