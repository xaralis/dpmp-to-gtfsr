import datetime as dt
import zipfile
from pathlib import Path

import pytest

from dpmp_gtfs.cis.index import build_index

FIXTURES = Path(__file__).parent / "fixtures" / "netex"


def _archive(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "netex.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.write(FIXTURES / name, arcname=name)
    return path


def test_later_valid_from_wins(tmp_path):
    archive = _archive(tmp_path, "line-655001-v1.xml", "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    line = index.lines["655001"]
    assert line.valid_from == dt.date(2026, 7, 1)
    # v1's third trip (9) must not leak in -- unioning versions would invent it.
    assert set(line.trips) == {1, 2}


def test_direction_comes_from_the_journey_pattern(tmp_path):
    archive = _archive(tmp_path, "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    trips = index.lines["655001"].trips
    assert trips[1] == 0  # _out
    assert trips[2] == 1  # _in


def test_versions_not_yet_valid_are_ignored(tmp_path):
    archive = _archive(tmp_path, "line-655001-v1.xml", "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 3, 1))

    line = index.lines["655001"]
    assert line.valid_from == dt.date(2026, 1, 1)
    assert set(line.trips) == {1, 2, 9}


def test_files_from_other_operators_are_skipped(tmp_path):
    other = tmp_path / "other.xml"
    v2_text = (FIXTURES / "line-655001-v2.xml").read_text(encoding="utf8")
    other.write_text(v2_text.replace("63217066", "11111111"), encoding="utf8")
    path = tmp_path / "netex.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.write(other, arcname="other.xml")

    index = build_index([path], on_date=dt.date(2026, 8, 10))
    assert index.lines == {}


def test_unknown_line_raises_keyerror(tmp_path):
    archive = _archive(tmp_path, "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))
    with pytest.raises(KeyError):
        index.lines["655999"]


def test_expired_version_loses_to_earlier_covering_version(tmp_path):
    # "expired" has a later FromDate than v1 but its ToDate is already in the
    # past by on_date -- it must not win just because its FromDate is later.
    archive = _archive(tmp_path, "line-655001-v1.xml", "line-655001-expired.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    line = index.lines["655001"]
    assert line.valid_from == dt.date(2026, 1, 1)
    assert set(line.trips) == {1, 2, 9}


def test_tie_on_from_date_resolved_by_longer_to_date(tmp_path):
    archive = _archive(tmp_path, "line-655001-tie-short.xml", "line-655001-tie-long.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    line = index.lines["655001"]
    assert line.valid_to == dt.date(2030, 12, 31)
    assert set(line.trips) == {22}


def test_tie_on_from_date_and_to_date_resolved_by_source_name(tmp_path):
    archive = _archive(tmp_path, "line-655001-tie-a.xml", "line-655001-tie-z.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    line = index.lines["655001"]
    # Deterministic, not "whichever the zip yielded first": the greater
    # source name (archive:entry) wins.
    assert set(line.trips) == {2}
    assert line.source.endswith("tie-z.xml")

    # Order in the archive must not change the outcome.
    reversed_dir = tmp_path / "reversed"
    reversed_dir.mkdir()
    reversed_archive = _archive(reversed_dir, "line-655001-tie-z.xml", "line-655001-tie-a.xml")
    reversed_index = build_index([reversed_archive], on_date=dt.date(2026, 8, 10))
    assert set(reversed_index.lines["655001"].trips) == {2}


def test_unresolved_pattern_ref_defaults_to_outbound_and_warns(tmp_path, caplog):
    archive = _archive(tmp_path, "line-655001-badref.xml")
    with caplog.at_level("WARNING"):
        index = build_index([archive], on_date=dt.date(2026, 8, 10))

    trips = index.lines["655001"].trips
    assert trips[1] == 0  # defaulted to OUTBOUND, not dropped
    assert any("655001" in r.message and "unresolved" in r.message for r in caplog.records)
