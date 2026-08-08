"""Reading tables back out of a built GTFS archive.

Three parts of the service read the published ``gtfs.zip`` rather than the
builder's own objects: the realtime index, the coverage map and the version
banner. Reading the artefact is deliberate -- it guarantees that what realtime
references is what consumers actually downloaded -- but it was previously done
three times with three different error policies, so a malformed archive
behaved differently depending on which caller reached it first.
"""

import csv
import io
import zipfile
from pathlib import Path

Row = dict[str, str]


def read_table(archive: zipfile.ZipFile, name: str) -> list[Row]:
    """Every row of one GTFS table, or nothing if the table is absent.

    A missing table is not an error: ``shapes.txt`` is optional, and
    ``stops.txt`` only supplies display names to the realtime index. Callers
    that genuinely require a table notice when it comes back empty.
    """
    try:
        with archive.open(name) as fh:
            return list(csv.DictReader(io.TextIOWrapper(fh, "utf8")))
    except KeyError:
        return []


def read_tables(path: Path, *names: str) -> dict[str, list[Row]]:
    """Read several tables in one pass, opening the archive once."""
    with zipfile.ZipFile(path) as zf:
        return {name: read_table(zf, name) for name in names}
