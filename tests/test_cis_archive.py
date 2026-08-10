import httpx
import pytest

from dpmp_gtfs.cis.archive import CisUnavailable, fetch_archives

URL = "https://portal.cisjr.cz/pub/netex/Test.zip"


async def test_downloads_and_records_last_modified(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"zipbytes",
            headers={"Last-Modified": "Fri, 07 Aug 2026 19:54:35 GMT"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        paths = await fetch_archives([URL], tmp_path, client=c)

    assert paths[0].read_bytes() == b"zipbytes"
    assert (tmp_path / "Test.zip.meta").exists()


async def test_sends_if_modified_since_and_keeps_the_cache_on_304(tmp_path):
    (tmp_path / "Test.zip").write_bytes(b"cached")
    (tmp_path / "Test.zip.meta").write_text("Fri, 07 Aug 2026 19:54:35 GMT", encoding="utf8")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        paths = await fetch_archives([URL], tmp_path, client=c)

    assert seen[0].headers["If-Modified-Since"] == "Fri, 07 Aug 2026 19:54:35 GMT"
    assert paths[0].read_bytes() == b"cached"


async def test_falls_back_to_cache_when_cis_is_down(tmp_path, caplog):
    (tmp_path / "Test.zip").write_bytes(b"cached")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        paths = await fetch_archives([URL], tmp_path, client=c)

    assert paths[0].read_bytes() == b"cached"
    assert "falling back" in caplog.text.lower()


async def test_raises_when_down_with_no_cache(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(CisUnavailable):
            await fetch_archives([URL], tmp_path, client=c)
