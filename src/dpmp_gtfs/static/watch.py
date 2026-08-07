"""Tracks which stops lose and regain service between rebuilds.

A stop with no departures is dropped from the feed -- publishing a stop no
vehicle reaches misleads passengers. But "nothing calls here" is not
necessarily permanent: a diversion or engineering work takes stops out of
service temporarily, and they come back.

So the dropped set is remembered across rebuilds and compared. A stop that
newly falls out of service, or quietly returns, is worth surfacing: it usually
means a diversion started or ended, which is exactly the sort of change that
should not pass unnoticed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceChange:
    """Stops whose service status changed since the previous build."""

    lost: dict[str, str] = field(default_factory=dict)
    """Stop id -> name, for stops that had service last time and now do not."""
    regained: dict[str, str] = field(default_factory=dict)
    """Stop id -> name, for stops back in service."""

    @property
    def any(self) -> bool:
        return bool(self.lost or self.regained)


@dataclass(frozen=True, slots=True)
class UnservedStops:
    """The set of stops with no scheduled service, as of one build."""

    recorded_at: dt.datetime
    stops: dict[str, str]
    """Stop id -> stop name."""

    def to_json(self) -> str:
        return json.dumps(
            {"recorded_at": self.recorded_at.isoformat(), "stops": self.stops},
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> UnservedStops:
        payload = json.loads(raw)
        return cls(
            recorded_at=dt.datetime.fromisoformat(payload["recorded_at"]),
            stops=dict(payload["stops"]),
        )

    def compare(self, previous: UnservedStops | None) -> ServiceChange:
        """What changed relative to an earlier snapshot.

        With no earlier snapshot there is nothing to compare against, so no
        change is reported -- a first run should not look like a network-wide
        diversion.
        """
        if previous is None:
            return ServiceChange()
        return ServiceChange(
            lost={k: v for k, v in self.stops.items() if k not in previous.stops},
            regained={k: v for k, v in previous.stops.items() if k not in self.stops},
        )


def load(path: Path) -> UnservedStops | None:
    if not path.exists():
        return None
    try:
        return UnservedStops.from_json(path.read_text(encoding="utf8"))
    except json.JSONDecodeError, KeyError, ValueError:
        # Corrupt state must not stop a rebuild; the worst case is one missed
        # comparison.
        logger.warning("could not read previous unserved-stop state at %s", path)
        return None


def save(path: Path, state: UnservedStops) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json(), encoding="utf8")


def report(change: ServiceChange) -> None:
    """Log service changes at a level that will actually be noticed."""
    if change.lost:
        logger.warning(
            "%d stop(s) lost all service since the last build, which may mean a "
            "diversion or closure: %s",
            len(change.lost),
            ", ".join(f"{name} ({sid})" for sid, name in sorted(change.lost.items())),
        )
    if change.regained:
        logger.warning(
            "%d stop(s) are back in service: %s",
            len(change.regained),
            ", ".join(f"{name} ({sid})" for sid, name in sorted(change.regained.items())),
        )
    if not change.any:
        logger.info("no change in which stops are served")
