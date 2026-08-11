"""Tests for the command line entry points."""

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dpmp_gtfs import cli


def test_build_static_refuses_when_cis_describes_no_calendars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler already refuses this; the CLI writes the same file and
    must refuse it too.

    Falling back for every trip would mean publishing the API's days of
    operation, which are wrong for about a third of them -- and doing it by
    overwriting a gtfs.zip that was right.
    """
    existing = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(existing, "w") as zf:
        zf.writestr("agency.txt", "the previous, good feed")
    before = existing.read_bytes()

    async def no_archives(*args: object, **kwargs: object) -> list[Path]:
        return []

    def no_calendars(*args: object, **kwargs: object) -> dict[tuple[str, int], frozenset[object]]:
        return {}

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the crawl must not start once the calendars are known to be missing")

    monkeypatch.setattr(cli, "fetch_archives", no_archives)
    monkeypatch.setattr(cli, "build_calendars", no_calendars)
    monkeypatch.setattr(cli, "crawl", must_not_run)

    result = CliRunner().invoke(cli.app, ["build-static", "--dest", str(existing)])

    assert result.exit_code == 1
    assert "no DPMP calendars" in result.output
    assert existing.read_bytes() == before, "the previous feed must survive untouched"
