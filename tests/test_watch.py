"""Tests for tracking stops that gain or lose service between rebuilds."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from dpmp_gtfs.static.watch import UnservedStops, load, save

NOW = dt.datetime(2026, 8, 7, 3, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(days=1)


def test_first_build_reports_no_change() -> None:
    """With nothing to compare against, a fresh install must not look like the
    whole network was diverted."""
    current = UnservedStops(NOW, {"S7P1": "Třída Míru"})
    assert not current.compare(None).any


def test_a_stop_falling_out_of_service_is_flagged() -> None:
    before = UnservedStops(NOW, {})
    after = UnservedStops(LATER, {"S7P1": "Třída Míru"})

    change = after.compare(before)
    assert change.lost == {"S7P1": "Třída Míru"}
    assert change.regained == {}
    assert change.any


def test_a_stop_returning_to_service_is_flagged() -> None:
    """The end of a diversion matters as much as its start."""
    before = UnservedStops(NOW, {"S7P1": "Třída Míru"})
    after = UnservedStops(LATER, {})

    change = after.compare(before)
    assert change.regained == {"S7P1": "Třída Míru"}
    assert change.lost == {}


def test_a_stop_out_of_service_the_whole_time_is_not_news() -> None:
    state = {"S7P1": "Třída Míru"}
    change = UnservedStops(LATER, state).compare(UnservedStops(NOW, state))
    assert not change.any


def test_state_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "unserved-stops.json"
    original = UnservedStops(NOW, {"S7P1": "Třída Míru", "S8P2": "U Grandu"})
    save(path, original)

    restored = load(path)
    assert restored is not None
    assert restored.stops == original.stops
    assert restored.recorded_at == original.recorded_at


def test_missing_state_file_is_not_an_error(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.json") is None


def test_corrupt_state_does_not_break_a_rebuild(tmp_path: Path) -> None:
    """A bad state file costs one comparison, not the whole build."""
    path = tmp_path / "unserved-stops.json"
    path.write_text("{ this is not json", encoding="utf8")
    assert load(path) is None
