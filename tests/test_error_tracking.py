"""Tests for error reporting.

What matters is less that the SDK starts than that the failures this service
actually has reach it. The scheduler catches a failed rebuild so the previous
feed keeps being served; caught and logged is exactly how such a failure
could go unreported for weeks.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from dpmp_gtfs import error_tracking
from dpmp_gtfs.config import Settings
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.types import Timetable
from dpmp_gtfs.web import scheduler as scheduler_module
from dpmp_gtfs.web.scheduler import Scheduler

DSN = "https://public@glitchtip.example/1"


class RecordingTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        if (event := envelope.get_event()) is not None:
            self.events.append(event)


@pytest.fixture(autouse=True)
def _no_sentry_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_RELEASE"):
        monkeypatch.delenv(name, raising=False)
    yield
    # Leave no active client behind for the rest of the suite.
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> RecordingTransport:
    """Run the real initialisation, but deliver to memory, not the network."""
    recording = RecordingTransport()
    real_init = sentry_sdk.init

    def init(*args: Any, **kwargs: Any) -> Any:
        return real_init(*args, transport=recording, **kwargs)

    monkeypatch.setattr(error_tracking.sentry_sdk, "init", init)
    monkeypatch.setenv("SENTRY_DSN", DSN)
    return recording


def test_nothing_starts_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("sentry_sdk.init must not be called without SENTRY_DSN")

    monkeypatch.setattr(error_tracking.sentry_sdk, "init", must_not_run)

    error_tracking.init_error_tracking()

    assert not sentry_sdk.get_client().is_active()


def test_reports_errors_only_and_nothing_personal(transport: RecordingTransport) -> None:
    error_tracking.init_error_tracking()

    options = sentry_sdk.get_client().options
    assert options["dsn"] == DSN
    assert options["environment"] == "production"
    assert options["send_default_pii"] is False
    assert options["traces_sample_rate"] is None
    assert options["profiles_sample_rate"] is None


def test_environment_and_release_come_from_the_deployment(
    transport: RecordingTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("SENTRY_RELEASE", "abc1234")

    error_tracking.init_error_tracking()

    options = sentry_sdk.get_client().options
    assert options["environment"] == "staging"
    assert options["release"] == "abc1234"


async def test_a_failed_rebuild_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_cis: object,
    transport: RecordingTransport,
) -> None:
    """The case this was added for: the nightly crawl failing on one trip,
    caught by the scheduler, and so far visible only in the pod's log."""
    error_tracking.init_error_tracking()

    async def failing_crawl(api: Any, calendars: Any) -> Timetable:
        raise DpmpApiError("connections/811/1 failed after 4 attempts")

    monkeypatch.setattr(scheduler_module, "crawl", failing_crawl)

    await Scheduler(Settings(data_dir=tmp_path, shapes_enabled=False)).rebuild_static()
    sentry_sdk.flush()

    [event] = transport.events
    assert event["level"] == "error"
    [exception] = event["exception"]["values"]
    assert exception["type"] == "DpmpApiError"
    assert "connections/811/1" in exception["value"]
