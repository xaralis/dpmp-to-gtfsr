"""Tests for the build-phase announcements around ``rebuild_static``.

A cold start discovers and fetches ~2,700 trips before it can answer
anything, several minutes during which the service would otherwise look
hung. ``Scheduler._phase`` is the single point that tells an operator
watching the log and a user watching the map the same thing, so what matters
here is that the phase is visible *during* each stage and gone once the
build ends, however it ends.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from dpmp_gtfs.config import Settings
from dpmp_gtfs.types import Timetable
from dpmp_gtfs.web import scheduler as scheduler_module
from dpmp_gtfs.web.scheduler import Scheduler


def _settings(tmp_path: Path, *, shapes_enabled: bool = False) -> Settings:
    return Settings(data_dir=tmp_path, shapes_enabled=shapes_enabled)


async def test_phase_writes_state_and_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sched = Scheduler(_settings(tmp_path))

    with caplog.at_level(logging.INFO, logger="dpmp_gtfs.web.scheduler"):
        sched._phase("stahuji jízdní řády")

    assert sched.state.static_phase == "stahuji jízdní řády"
    assert "stahuji jízdní řády" in caplog.text


async def test_rebuild_static_announces_the_crawl_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase must already be set *before* the network call it describes,
    not logged around it afterwards -- otherwise a caller inspecting state
    mid-crawl would see nothing."""
    seen: list[str | None] = []
    sched = Scheduler(_settings(tmp_path))

    async def fake_crawl(api: Any) -> Timetable:
        seen.append(sched.state.static_phase)
        return Timetable(stops=[], lines=[])

    monkeypatch.setattr(scheduler_module, "crawl", fake_crawl)

    await sched.rebuild_static()

    assert seen == ["stahuji jízdní řády"]
    # An empty timetable has no services to publish; that failure is not
    # what this test is about, only that the phase was set and then cleared.
    assert sched.state.static_phase is None


async def test_rebuild_static_announces_the_shapes_phase_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []
    sched = Scheduler(_settings(tmp_path, shapes_enabled=True))

    async def fake_crawl(api: Any) -> Timetable:
        return Timetable(stops=[], lines=[])

    def fake_with_shapes(feed: Any, cache_path: Path, router: Any = None) -> Any:
        seen.append(sched.state.static_phase)
        return feed

    monkeypatch.setattr(scheduler_module, "crawl", fake_crawl)
    monkeypatch.setattr(scheduler_module, "with_shapes", fake_with_shapes)

    await sched.rebuild_static()

    assert seen == ["počítám trasy"]
    assert sched.state.static_phase is None


async def test_shapes_phase_is_skipped_when_shapes_are_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False
    sched = Scheduler(_settings(tmp_path, shapes_enabled=False))

    async def fake_crawl(api: Any) -> Timetable:
        return Timetable(stops=[], lines=[])

    def fake_with_shapes(feed: Any, cache_path: Path, router: Any = None) -> Any:
        nonlocal called
        called = True
        return feed

    monkeypatch.setattr(scheduler_module, "crawl", fake_crawl)
    monkeypatch.setattr(scheduler_module, "with_shapes", fake_with_shapes)

    await sched.rebuild_static()

    assert called is False
    assert sched.state.static_phase is None


async def test_the_phase_is_cleared_even_when_the_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that errors out must not leave the map claiming forever that
    something is still loading."""
    sched = Scheduler(_settings(tmp_path))

    async def broken_crawl(api: Any) -> Timetable:
        raise RuntimeError("upstream is gone")

    monkeypatch.setattr(scheduler_module, "crawl", broken_crawl)

    await sched.rebuild_static()

    assert sched.state.static_phase is None
    assert sched.state.static_error is not None


async def test_a_second_rebuild_is_skipped_while_one_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The initial build and the nightly loop's are now scheduled
    independently (see ``_build_lock``'s docstring): a cold start shortly
    before ``static_rebuild_hour`` can have both want to run at once. Only
    one crawl -- and one write to ``self.state`` -- must actually happen."""
    gate = asyncio.Event()
    starts = 0

    async def gated_crawl(api: Any) -> Timetable:
        nonlocal starts
        starts += 1
        await gate.wait()
        return Timetable(stops=[], lines=[])

    monkeypatch.setattr(scheduler_module, "crawl", gated_crawl)
    sched = Scheduler(_settings(tmp_path))

    first = asyncio.create_task(sched.rebuild_static())
    await asyncio.sleep(0)  # let the first call reach the crawl and take the lock
    assert sched._build_lock.locked()

    with caplog.at_level(logging.INFO, logger="dpmp_gtfs.web.scheduler"):
        await sched.rebuild_static()  # a second call while the first is still running

    assert starts == 1, "the crawl must not have started a second time"
    assert "skipping" in caplog.text.lower()

    gate.set()
    await first
    assert not sched._build_lock.locked()
