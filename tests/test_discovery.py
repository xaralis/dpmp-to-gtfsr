from dpmp_gtfs.static.discovery import discover_trips


class FakeApi:
    def __init__(self, present: set[int]) -> None:
        self.present = present
        self.asked: list[int] = []

    async def connection(self, line: str, number: int):
        self.asked.append(number)
        return object() if number in self.present else None


async def test_finds_a_contiguous_run():
    api = FakeApi({1, 2, 3})
    assert await discover_trips(api, "1", stop_after=5) == [1, 2, 3]


async def test_crosses_gaps_smaller_than_the_stop_rule():
    # The largest gap measured anywhere in the real network is 18.
    api = FakeApi({1, 20, 21})
    assert await discover_trips(api, "1", stop_after=25) == [1, 20, 21]


async def test_stops_after_enough_consecutive_misses():
    api = FakeApi({1})
    assert await discover_trips(api, "1", stop_after=5) == [1]
    assert max(api.asked) == 6


async def test_an_empty_line_yields_nothing():
    api = FakeApi(set())
    assert await discover_trips(api, "99", stop_after=3) == []
