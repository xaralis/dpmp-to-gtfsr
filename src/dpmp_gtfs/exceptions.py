"""Exceptions raised across the project.

Collected here so a caller can catch what it cares about without importing the
module that happens to raise it -- the web layer wants to know that a build
failed, not that the failure came from the routing client.
"""


class DpmpGtfsError(Exception):
    """Base for anything this project raises deliberately.

    Lets a caller distinguish a failure this code anticipated from a bug.
    """


class DpmpApiError(DpmpGtfsError):
    """The upstream API could not be reached, or answered with something
    unusable."""


class RoutingError(DpmpGtfsError):
    """The router could not produce geometry for a stop sequence.

    Never fatal: a feed without ``shapes.txt`` is degraded but valid.
    """


class FeedBuildError(DpmpGtfsError):
    """A built feed failed its own consistency checks and must not be
    published."""
