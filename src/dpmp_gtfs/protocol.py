"""The rotating signature api.mhdonline.cz expects in ``X-App-Protocol``.

Not a secret. The seed below is what their web bundle ships -- a placeholder
from whatever template they built on, left in production. It is carried in
settings anyway so that replacing it is a restart rather than a commit, the
same reasoning that applied to the old API key.
"""

import hashlib
import hmac
import time

PROTOCOL_WINDOW_MS = 15 * 60 * 1000
"""How long one signature stays valid. A full crawl outlasts this, so callers
must recompute per request rather than once per client."""


def app_protocol(seed: str, now_ms: int | None = None) -> str:
    """The signature for the window containing ``now_ms``."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    counter = now_ms // PROTOCOL_WINDOW_MS
    return hmac.new(seed.encode(), str(counter).encode(), hashlib.sha256).hexdigest()
