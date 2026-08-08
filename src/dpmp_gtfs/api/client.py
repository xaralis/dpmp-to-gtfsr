"""Async client for the DPMP API.

Two upstream quirks drive the shape of this module:

1. Every call is a ``POST`` whose body is ``{"key": "..."}`` -- and it must be
   sent as ``text/plain``. With ``Content-Type: application/json`` the server
   answers 500. (The web app hits this by accident: ``fetch`` with a plain
   string body defaults to ``text/plain``.)

2. The upstream is not robust. During exploration it twice stopped answering
   for minutes at a time, then recovered. Retries are mandatory, and callers
   must be able to survive a total outage.
"""

import asyncio
import json
import logging
from types import TracebackType
from typing import Any, Self

import httpx

from dpmp_gtfs.config import Settings
from dpmp_gtfs.config import settings as default_settings
from dpmp_gtfs.exceptions import DpmpApiError

from .models import Bus, BusesResponse, Code, ConnectionDetail, ConnectionSummary, Line, Station

logger = logging.getLogger(__name__)


class DpmpApiClient:
    """Talks to ``online.dpmp.cz/api``.

    Use as an async context manager so the underlying connection pool is closed:

        async with DpmpApiClient() as api:
            buses = await api.buses()
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or default_settings
        if not self.settings.api_key:
            raise DpmpApiError(
                "No API key configured. Set DPMP_API_KEY -- the public web app "
                "ships one in its JS bundle."
            )
        self._body = json.dumps({"key": self.settings.api_key})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.api_root,
            timeout=self.settings.request_timeout,
            headers={"User-Agent": self.settings.user_agent},
        )
        self._gate = asyncio.Semaphore(self.settings.crawl_concurrency)

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

    async def _post(self, path: str, **params: Any) -> Any:
        """POST to an endpoint, retrying with exponential backoff.

        Raises :class:`DpmpApiError` once retries are exhausted, so callers can
        distinguish "upstream is down" from a bug.
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
                async with self._gate:
                    response = await self._client.post(
                        f"/{path}",
                        params=params or None,
                        content=self._body,
                        # Not application/json -- see module docstring.
                        headers={"Content-Type": "text/plain;charset=UTF-8"},
                    )
                    if self.settings.crawl_delay:
                        await asyncio.sleep(self.settings.crawl_delay)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last = exc

        raise DpmpApiError(
            f"{path} failed after {self.settings.max_retries} attempts: {last!r}"
        ) from last

    # -- endpoints -----------------------------------------------------------

    async def codes(self) -> list[Code]:
        return [Code.model_validate(c) for c in await self._post("codes")]

    async def stations(self) -> list[Station]:
        return [Station.model_validate(s) for s in await self._post("stations")]

    async def lines(self) -> list[Line]:
        lines = [Line.model_validate(line) for line in await self._post("lines")]
        return sorted(lines, key=lambda line: line.number)

    async def connections(self, line: int) -> list[ConnectionSummary]:
        payload = await self._post("connections", line=line)
        return [ConnectionSummary.model_validate(c) for c in payload]

    async def connection_detail(self, line: int, number: int) -> ConnectionDetail:
        payload = await self._post("connectionDetail", line=line, number=number)
        return ConnectionDetail.model_validate(payload)

    async def buses(self) -> list[Bus]:
        payload = BusesResponse.model_validate(await self._post("buses"))
        if not payload.success:
            raise DpmpApiError("buses endpoint reported success=false")
        return payload.data

    async def events(self) -> list[dict[str, Any]]:
        """Service disruptions.

        Returned untyped on purpose: the endpoint has only ever been observed
        empty, so its element shape is still unknown. Typing it now would be
        guessing.
        """
        payload = await self._post("events")
        return list(payload)
