"""GTFS identifier construction.

Kept in one place because both the static builder and the realtime feed have
to agree on them exactly -- a mismatch means realtime silently referencing
trips that consumers cannot resolve.
"""

AGENCY_ID = "DPMP"


def station_id(station: int) -> str:
    """Parent station, e.g. ``S16``."""
    return f"S{station}"


def stop_id(station: int, platform: int) -> str:
    """Boarding platform, e.g. ``S16P2``.

    The API encodes the same thing as ``station * 100 + platform`` in
    ``current_stop_number``; this spells it out instead so the ids stay
    readable and unambiguous past 99 platforms.
    """
    return f"S{station}P{platform}"


def route_id(line: str) -> str:
    return f"L{line}"


def trip_id(line: str, connection: int) -> str:
    """A trip, e.g. ``L9C115``.

    ``connection`` is the upstream's own trip number, which ``/api/buses``
    reports directly as ``connection_no`` -- so realtime maps onto static
    without any guesswork.
    """
    return f"L{line}C{connection}"
