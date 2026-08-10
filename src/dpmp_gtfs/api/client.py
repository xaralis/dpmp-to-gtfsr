"""Async client for api.mhdonline.cz.

Three upstream facts drive the shape of this module:

1. Every call is a plain ``GET`` under ``/{provider}/``. The old API's
   ``POST`` with a ``text/plain`` body is gone, and so is its static key.

2. Authentication is a signature that rotates every 15 minutes
   (:mod:`dpmp_gtfs.protocol`). A full crawl takes longer than that, so the
   header is computed per request, never cached on the client. A 401 or 403
   is treated as "the window rolled under us" and retried once with a fresh
   signature.

3. ``connections/{line}/{number}`` answers 404 for a trip number that does
   not exist. That is data, not failure -- CIS and the API drift -- so it is
   returned as ``None`` rather than raised.
"""

import asyncio
import json
import logging
import time
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import ValidationError

from dpmp_gtfs.config import Settings
from dpmp_gtfs.config import settings as default_settings
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.protocol import app_protocol

from .models import Connection, Line, Stop, Vehicle, VehiclesResponse

logger = logging.getLogger(__name__)

AUTH_STATUSES = frozenset({401, 403})


class RateLimiter:
    """Lets through at most ``rate`` requests per second, however many callers.

    A fixed sleep between requests would not do: with N workers in flight the
    real rate depends on latency, so the one number we actually care about
    would drift with the network.
    """

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now += wait
            self._next = max(now, self._next) + self._interval


class DpmpApiClient:
    """Talks to ``api.mhdonline.cz/{provider}``.

    Use as an async context manager so the connection pool is closed:

        async with DpmpApiClient() as api:
            snapshot = await api.vehicles()  # VehiclesResponse: .time, .vehicles
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or default_settings
        self._prefix = f"/{self.settings.provider}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.api_root,
            timeout=self.settings.request_timeout,
            headers={"User-Agent": self.settings.user_agent},
        )
        self._gate = asyncio.Semaphore(self.settings.crawl_concurrency)
        self._limiter = RateLimiter(self.settings.crawl_rate_limit)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- transport -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "X-App-Protocol": app_protocol(self.settings.protocol_seed),
            "Accept": "application/json",
        }

    async def _send(self, path: str) -> httpx.Response:
        async with self._gate:
            await self._limiter.acquire()
            response = await self._client.get(f"{self._prefix}/{path}", headers=self._headers())
        if response.status_code in AUTH_STATUSES:
            # The 15-minute window rolled over mid-flight. One fresh attempt.
            logger.debug("%s got %d, retrying with a new signature", path, response.status_code)
            async with self._gate:
                await self._limiter.acquire()
                response = await self._client.get(
                    f"{self._prefix}/{path}", headers=self._headers()
                )
        return response

    async def _get(self, path: str, *, missing_ok: bool = False) -> Any:
        """GET an endpoint, retrying with exponential backoff.

        Returns ``None`` for a 404 when ``missing_ok`` -- see the module
        docstring. Raises :class:`DpmpApiError` once retries are exhausted, so
        callers can tell "upstream is down" from a bug.
        """
        last: Exception | None = None

        for attempt in range(self.settings.max_retries):
            if attempt:
                delay = self.settings.retry_backoff**attempt
                logger.warning(
                    "%s failed (%s), retry %d/%d in %.0fs",
                    path,
                    type(last).__name__,
                    attempt,
                    self.settings.max_retries - 1,
                    delay,
                )
                await asyncio.sleep(delay)

            try:
                response = await self._send(path)
                if missing_ok and response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last = exc

        raise DpmpApiError(
            f"{path} failed after {self.settings.max_retries} attempts: {last!r}"
        ) from last

    # -- endpoints -----------------------------------------------------------

    async def stops(self) -> list[Stop]:
        return [Stop.model_validate(s) for s in await self._get("stops")]

    async def lines(self) -> list[Line]:
        lines = [Line.model_validate(line) for line in await self._get("lines")]
        return sorted(lines, key=lambda line: line.jdf_id)

    async def vehicles(self) -> VehiclesResponse:
        """The current snapshot, tolerant of individual bad records.

        ``VehiclesResponse.model_validate`` on the whole payload would reject
        every vehicle in the response over one malformed entry -- the same
        failure shape the ``/stops`` missing-coordinate case had. A stray
        vehicle is data, not an outage, so it is logged and dropped rather
        than taking the rest of the fleet down with it.
        """
        payload = await self._get("vehicles")
        vehicles: list[Vehicle] = []
        for raw in payload.get("vehicles", []):
            try:
                vehicles.append(Vehicle.model_validate(raw))
            except ValidationError as exc:
                logger.warning(
                    "skipping vehicle %r that failed validation: %s", raw.get("vid"), exc
                )
        return VehiclesResponse.model_validate({**payload, "vehicles": vehicles})

    async def connection(self, line: str, number: int) -> Connection | None:
        """One trip's stop times, or ``None`` if the upstream has no such trip."""
        payload = await self._get(f"connections/{line}/{number}", missing_ok=True)
        return None if payload is None else Connection.model_validate(payload)

    async def events(self) -> list[dict[str, Any]]:
        """Service disruptions.

        Returned untyped on purpose: the endpoint has only ever been observed
        empty, on both the old API and this one, so its element shape is still
        unknown. Typing it now would be guessing.
        """
        return list(await self._get("events"))
