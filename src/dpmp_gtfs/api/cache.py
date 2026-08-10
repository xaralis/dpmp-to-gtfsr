"""A short-lived on-disk cache of API responses, for development only.

A full crawl is roughly 4,400 requests over about twenty minutes, and most of
that is the trip-number probing -- the run needs ~50 consecutive 404s to
convince itself a line has no higher trip numbers. Debugging anything that
happens at the *end* of a build therefore costs twenty minutes per attempt,
which is why this exists.

Three properties are load-bearing:

*Misses are cached too.* Around a third of a crawl's requests are 404s. A
cache that stored only successes would leave the slowest, most repetitive part
of the run untouched.

*The key is the request path alone.* The ``X-App-Protocol`` signature rotates
every fifteen minutes; folding it into the key would silently expire the whole
cache four times an hour and make this look broken rather than useless.

*How long an entry lives depends on what it describes.* The timetable moves in
weeks, vehicle positions in seconds. One TTL for both would either make a
crawl useless or freeze the live map, so each path gets the staleness it can
actually tolerate -- and anything unrecognised gets the cautious one.

Off unless ``DPMP_HTTP_CACHE`` is enabled. Production rebuilds want the live
timetable, not one from twelve hours ago.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MISS = object()
"""Returned by :meth:`ResponseCache.get` when there is nothing usable stored.

A distinct sentinel rather than ``None``, because ``None`` is a perfectly good
cached value -- it is what a cached 404 replays as.
"""

SETTLED_TTL = 12 * 60 * 60
"""For anything that changes on the scale of a timetable revision."""

VOLATILE_TTL = 5 * 60
"""For anything describing the network right now."""

SETTLED_PREFIXES = ("stops", "lines", "connections/")
"""Paths that earn :data:`SETTLED_TTL`.

Deliberately an allow-list rather than a deny-list of volatile paths. An
endpoint nobody here has heard of yet gets :data:`VOLATILE_TTL`, so the worst a
future addition can do is be five minutes stale -- whereas a deny-list would
hand it half a day and let it look frozen.
"""


def ttl_for(path: str) -> float:
    """How long a cached response for ``path`` stays usable, in seconds."""
    return SETTLED_TTL if path.startswith(SETTLED_PREFIXES) else VOLATILE_TTL


class ResponseCache:
    """Read-through cache keyed by request path, expiring by file mtime."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _entry(self, path: str) -> Path:
        digest = hashlib.sha256(path.encode()).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, path: str) -> Any:
        """The stored payload, or :data:`MISS`."""
        entry = self._entry(path)
        try:
            age = time.time() - entry.stat().st_mtime
        except OSError:
            return MISS
        if age > ttl_for(path):
            return MISS
        try:
            stored = json.loads(entry.read_text(encoding="utf8"))
        except OSError, ValueError:
            # A truncated entry is not worth repairing; treat it as absent.
            return MISS
        return stored["payload"]

    def put(self, path: str, payload: Any) -> None:
        entry = self._entry(path)
        # Same atomic write as the feed archive: a crawl interrupted mid-write
        # must not leave an entry that later reads as valid.
        tmp = entry.with_suffix(".tmp")
        tmp.write_text(json.dumps({"path": path, "payload": payload}), encoding="utf8")
        os.replace(tmp, entry)
