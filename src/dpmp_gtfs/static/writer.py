"""Serialises a :class:`Feed` into a GTFS zip archive."""

import csv
import datetime as dt
import hashlib
import io
import logging
import os
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from dpmp_gtfs.exceptions import FeedBuildError
from dpmp_gtfs.ids import AGENCY_ID
from dpmp_gtfs.types import Feed

from .service_watch import write_unserved

logger = logging.getLogger(__name__)

AGENCY_ROWS = [
    {
        "agency_id": AGENCY_ID,
        "agency_name": "Dopravní podnik města Pardubic a.s.",
        "agency_url": "https://www.dpmp.cz",
        "agency_timezone": "Europe/Prague",
        "agency_lang": "cs",
        "agency_phone": "+420 466 899 111",
    }
]

FEED_PUBLISHER_NAME = "dpmp-to-gtfsr (neoficiální)"
FEED_PUBLISHER_URL = "https://github.com/xaralis/dpmp-to-gtfsr"


def write_feed(feed: Feed, destination: Path) -> str:
    """Write feed to ``destination`` atomically and return the content hash.

    Atomic because the HTTP layer serves this file straight off disk: a reader
    arriving mid-write would otherwise get a truncated archive.

    Recording which stops lost service is part of publishing, not a separate
    step a caller might forget or point somewhere else -- which is exactly what
    happened while the two were called side by side.
    """
    files = feed_to_files(feed)
    files_content_hash = content_hash(files)

    files["feed_info.txt"] = _csv(
        [
            {
                "feed_publisher_name": FEED_PUBLISHER_NAME,
                "feed_publisher_url": FEED_PUBLISHER_URL,
                "feed_lang": "cs",
                "feed_start_date": _date(feed.start_date),
                "feed_end_date": _date(feed.end_date),
                "feed_version": f"{_date(dt.date.today())}-{files_content_hash}",
                "feed_contact_url": f"{FEED_PUBLISHER_URL}/issues",
            }
        ],
        (
            "feed_publisher_name",
            "feed_publisher_url",
            "feed_lang",
            "feed_start_date",
            "feed_end_date",
            "feed_version",
            "feed_contact_url",
        ),
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])

    os.replace(tmp, destination)
    write_unserved(destination.parent, feed.unserved_stops)

    logger.info(
        "wrote %s (%d B, version %s)",
        destination,
        destination.stat().st_size,
        files_content_hash,
    )
    return files_content_hash


def feed_to_files(feed: Feed) -> dict[str, bytes]:
    """Render every GTFS table. Returned in memory so the caller can hash the
    result before deciding whether it is worth republishing."""
    stop_columns = (
        "stop_id",
        "stop_name",
        "stop_lat",
        "stop_lon",
        "location_type",
        "parent_station",
        "platform_code",
        "wheelchair_boarding",
    )
    route_columns = ("route_id", "route_short_name", "route_long_name", "route_type")
    trip_columns = (
        "route_id",
        "service_id",
        "trip_id",
        "trip_headsign",
        "direction_id",
        "wheelchair_accessible",
        "shape_id",
    )
    stop_time_columns = (
        "trip_id",
        "arrival_time",
        "departure_time",
        "stop_id",
        "stop_sequence",
        "pickup_type",
        "drop_off_type",
        "shape_dist_traveled",
    )

    # A feed with no services is not a degraded feed, it is a failed crawl:
    # every trip would be unreachable. Say so here, rather than dying on an
    # IndexError further down and reporting that as the cause.
    if not feed.services:
        raise FeedBuildError("feed has no services; the crawl returned no usable trips")

    # Force static agency_id to every route, since the feed has only one agency.
    routes = [{**r, "agency_id": AGENCY_ID} for r in _as_csv_rows(feed.routes, route_columns)]
    calendar_rows = []

    for service in feed.services:
        mon, tue, wed, thu, fri, sat, sun = service.weekday_flags
        calendar_rows.append(
            {
                "service_id": service.service_id,
                "monday": mon,
                "tuesday": tue,
                "wednesday": wed,
                "thursday": thu,
                "friday": fri,
                "saturday": sat,
                "sunday": sun,
                "start_date": _date(feed.start_date),
                "end_date": _date(feed.end_date),
            }
        )

    files = {
        "agency.txt": _csv(AGENCY_ROWS, list(AGENCY_ROWS[0])),
        "stops.txt": _csv(_as_csv_rows(feed.stops, stop_columns), stop_columns),
        "routes.txt": _csv(routes, ("route_id", "agency_id", *route_columns[1:])),
        "trips.txt": _csv(_as_csv_rows(feed.trips, trip_columns), trip_columns),
        "stop_times.txt": _csv(_as_csv_rows(feed.stop_times, stop_time_columns), stop_time_columns),
        "calendar.txt": _csv(calendar_rows, list(calendar_rows[0])),
    }
    exception_rows = [
        {
            "service_id": e.service_id,
            "date": _date(e.date),
            "exception_type": 1 if e.added else 2,
        }
        for e in feed.calendar_exceptions
    ]

    if exception_rows:
        files["calendar_dates.txt"] = _csv(exception_rows, list(exception_rows[0]))

    # shapes.txt is optional, and the feed stays valid without it when the
    # router is unreachable.
    shape_columns = (
        "shape_id",
        "shape_pt_lat",
        "shape_pt_lon",
        "shape_pt_sequence",
        "shape_dist_traveled",
    )
    shape_rows = [
        {
            "shape_id": shape.shape_id,
            "shape_pt_lat": f"{lat:.6f}",
            "shape_pt_lon": f"{lon:.6f}",
            "shape_pt_sequence": sequence,
            "shape_dist_traveled": f"{distance:.1f}",
        }
        for shape in feed.shapes
        for sequence, ((lat, lon), distance) in enumerate(
            zip(shape.points, shape.point_distances, strict=True)
        )
    ]

    if shape_rows:
        files["shapes.txt"] = _csv(shape_rows, shape_columns)

    return files


def content_hash(files: dict[str, bytes]) -> str:
    """Stable digest of the timetable content.

    ``feed_info.txt`` is deliberately excluded from the hash, since it carries
    the build date and would otherwise make every rebuild look like a change.
    """
    digest = hashlib.sha256()

    for name in sorted(files):
        digest.update(name.encode())
        digest.update(files[name])

    return digest.hexdigest()[:16]


def _csv(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf8")


def _as_csv_rows(items: Iterable[Any], columns: Sequence[str]) -> list[dict[str, Any]]:
    return [{c: getattr(item, c) for c in columns} for item in items]


def _date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")
