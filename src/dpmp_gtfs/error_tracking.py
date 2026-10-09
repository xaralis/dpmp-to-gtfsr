"""Error reporting to a Sentry-compatible endpoint (GlitchTip in production).

Off unless ``SENTRY_DSN`` is set, so local runs and the test suite send
nothing. Errors only: no traces, no profiles.

What gets reported is mostly decided by log level, not by code here. The
logging integration turns every ``logger.error`` / ``logger.exception`` into
an event and keeps lower levels as breadcrumbs, which is what makes a failed
nightly rebuild visible -- the scheduler catches it so the previous feed keeps
being served, and logs it. So the rule for the rest of the code base is the
one it already follows: an upstream outage that will pass is a warning, a
failure somebody has to act on is an error.

The variables are the SDK's own names rather than ``DPMP_``-prefixed
settings: they describe where the service is deployed, not how it behaves.
"""

import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration


def init_error_tracking() -> None:
    """Start reporting, if ``SENTRY_DSN`` is set."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT") or "production",
        # Set in the image from the commit it was built from. Left unset, the
        # SDK falls back to asking git, which is what a local checkout wants.
        release=os.environ.get("SENTRY_RELEASE") or None,
        send_default_pii=False,
        traces_sample_rate=None,
        profiles_sample_rate=None,
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )
