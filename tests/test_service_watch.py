"""Tests for tracking stops that gain or lose service between rebuilds."""

import datetime as dt
from pathlib import Path

from dpmp_gtfs.static.service_watch import (
    UnservedStops,
    _save_state,
    load_unserved,
    state_path,
    write_unserved,
)

NOW = dt.datetime(2026, 8, 7, 3, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(days=1)


def test_first_build_reports_no_change() -> None:
    """With nothing to compare against, a fresh install must not look like the
    whole network was diverted."""
    current = UnservedStops(NOW, {"S7P1": "Třída Míru"})
    assert current.compare(None) is None


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
    _save_state(path, original)

    restored = load_unserved(path)
    assert restored is not None
    assert restored.stops == original.stops
    assert restored.recorded_at == original.recorded_at


def test_missing_state_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_unserved(tmp_path / "nope.json") is None


def test_corrupt_state_does_not_break_a_rebuild(tmp_path: Path) -> None:
    """A bad state file costs one comparison, not the whole build."""
    path = tmp_path / "unserved-stops.json"
    path.write_text("{ this is not json", encoding="utf8")
    assert load_unserved(path) is None


# --- where the state file lives ---------------------------------------------


def test_the_writer_and_the_reader_agree_on_the_path(tmp_path: Path) -> None:
    """Regression: they used to derive it independently and disagree.

    write_unserved took ``.parent`` of its argument, but the CLI passed a file
    (``data/gtfs.zip``) while the scheduler passed a directory (``data``). So
    the service wrote its state beside the working directory and read it back
    from the data directory: the comparison never ran, and under Docker the
    file fell outside the mounted volume and vanished on every restart.
    """
    write_unserved(tmp_path, {"S7P1": "Třída Míru"})

    assert state_path(tmp_path).exists()
    restored = load_unserved(state_path(tmp_path))
    assert restored is not None
    assert restored.stops == {"S7P1": "Třída Míru"}


def test_state_survives_a_round_trip_through_the_service_paths(tmp_path: Path) -> None:
    """What one build writes, the next process must find -- that is the whole
    point of the file."""
    write_unserved(tmp_path, {"S7P1": "Třída Míru"})
    first = load_unserved(state_path(tmp_path))

    write_unserved(tmp_path, {})
    second = load_unserved(state_path(tmp_path))

    assert first is not None and second is not None
    assert second.compare(first).regained == {"S7P1": "Třída Míru"}
