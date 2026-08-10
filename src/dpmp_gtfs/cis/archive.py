"""Fetches the NeTEx archives, and keeps the last good copy.

The two archives together are ~300 MB, and CIS republishes them roughly
weekly. A conditional request means a rebuild on an unchanged registry costs
one round trip per archive rather than the download.

A rebuild must survive CIS being down, so a cached archive is preferred over
no archive at all -- but a *stale* registry is announced loudly, because it
silently produces a feed for last week's timetable.
"""

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CHUNK = 1 << 20


class CisUnavailable(RuntimeError):  # noqa: N818 -- name fixed by the task interface
    """CIS could not be reached and nothing usable was cached."""


async def fetch_archives(
    urls: tuple[str, ...] | list[str],
    dest: Path,
    client: httpx.AsyncClient | None = None,
) -> list[Path]:
    """Download each archive into ``dest``, reusing unchanged copies."""
    dest.mkdir(parents=True, exist_ok=True)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=300.0, follow_redirects=True)
    try:
        return [await _fetch_one(client, url, dest) for url in urls]
    finally:
        if owns:
            await client.aclose()


async def _fetch_one(client: httpx.AsyncClient, url: str, dest: Path) -> Path:
    target = dest / url.rsplit("/", 1)[-1]
    meta = target.with_suffix(target.suffix + ".meta")

    headers: dict[str, str] = {}
    if target.exists() and meta.exists():
        headers["If-Modified-Since"] = meta.read_text(encoding="utf8").strip()

    try:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                logger.info("%s unchanged, using the cached copy", target.name)
                return target
            response.raise_for_status()
            with target.open("wb") as fh:
                async for chunk in response.aiter_bytes(CHUNK):
                    fh.write(chunk)
            if last_modified := response.headers.get("Last-Modified"):
                meta.write_text(last_modified, encoding="utf8")
        logger.info("downloaded %s (%d bytes)", target.name, target.stat().st_size)
        return target
    except httpx.HTTPError as exc:
        if target.exists():
            logger.warning(
                "CIS unreachable (%s); falling back to the cached %s from %s",
                exc,
                target.name,
                meta.read_text(encoding="utf8").strip() if meta.exists() else "an unknown date",
            )
            return target
        raise CisUnavailable(f"{url} is unreachable and nothing is cached: {exc!r}") from exc
