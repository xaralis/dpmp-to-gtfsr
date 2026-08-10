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
