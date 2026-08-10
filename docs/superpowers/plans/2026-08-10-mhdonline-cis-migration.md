# Migrace na api.mhdonline.cz a CIS — implementační plán

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Přepnout projekt ze zaniklého `online.dpmp.cz/api` na `api.mhdonline.cz` pro obsah a realtime a na CIS NeTEx pro seznam spojů, beze změny veřejných identifikátorů feedu.

**Architecture:** Dva zdroje s ostře oddělenou rolí. CIS NeTEx je rejstřík — říká, které spoje k danému datu existují a kterým směrem jedou. `api.mhdonline.cz` je obsah — zastávky se souřadnicemi, zastávkové časy, nástupiště a polohy vozidel. Potkávají se jediným klíčem: `lines[].jdfId` páruje `655001` s `lineId "1"`, a `connectionId` u vozidla je totéž číslo jako číslo spoje v JDF.

**Tech Stack:** Python 3.14, httpx (async), pydantic v2 + pydantic-settings, `defusedxml`, `zipfile`, protobuf (`gtfs_realtime_pb2`), pytest + pytest-asyncio, ruff, mypy strict.

## Global Constraints

- Python 3.14, `mypy --strict` musí projít; `packages = ["dpmp_gtfs"]`.
- Testy běží **bez sítě**. Vše přes nahrané fixtures v `tests/fixtures/`.
- `asyncio_mode = "auto"` — async testy se nemarkují.
- Podpis `X-App-Protocol` = `HMAC-SHA256(seed, str(floor(unix_ms / 900000)))` v hexu, seed `your-public-protocol-seed`, platnost 15 minut.
- Provozovatel DPMP v CIS = IČO `63217066`; linky `655xxx`.
- Rychlost dotazů na API: **8 req/s**, souběžnost 8.
- Veřejné identifikátory se **nesmí změnit**: `route_id` = `L{linka}`, `trip_id` = `L{linka}C{spoj}`, `stop_id` = `S{stanice}P{nástupiště}`, parent `S{stanice}`.
- `fixedCodes` se porovnávají **case-sensitive**: `X` (spoj, pracovní dny) ≠ `x` (zastávka, na znamení).
- Commituj po každém úkolu. Nikdy nepřidávej `Co-Authored-By`.
- NeTEx se parsuje **výhradně přes `defusedxml`**. Je to ~3,6 GB cizího XML zpracovaného v noční automatice bez dohledu; `xml.etree.ElementTree` je proti „billion laughs" a kvadratické expanzi entit bezbranný a jeden zlomyslný nebo jen vadný soubor by položil rebuild na OOM.

## Struktura souborů

**Nové**

| soubor | odpovědnost |
|---|---|
| `src/dpmp_gtfs/protocol.py` | výpočet rotujícího podpisu `X-App-Protocol` |
| `src/dpmp_gtfs/cis/__init__.py` | veřejné jméno balíku: `ServiceIndex`, `build_index`, `fetch_archives` |
| `src/dpmp_gtfs/cis/archive.py` | stažení NeTEx zipů přes httpx s `If-Modified-Since` |
| `src/dpmp_gtfs/cis/index.py` | parsování NeTEx, výběr verze, `ServiceIndex` |
| `tests/test_protocol.py`, `tests/test_cis_archive.py`, `tests/test_cis_index.py` | testy k výše uvedeným |
| `scripts/cross_validate.py` | porovnání nového feedu proti referenci ze starého API |

**Přepisované** — `api/client.py`, `api/models.py`, `static/crawler.py`, `static/calendar.py`, `upstream.py`, `config.py`, `types.py` (jen `Timetable`), `docs/upstream-api.md`

**Upravované** — `static/builder.py`, `realtime/feed.py`, `web/scheduler.py`, `cli.py`

**Mazané** — `src/dpmp_gtfs/realtime/tracker.py`, `tests/test_tracker.py`

---

### Task 1: Odložit starý feed jako referenci

Migrace přepíše `data/gtfs.zip`, což je jediná existující kopie feedu postaveného ze starého API. Bez ní nelze na konci nic křížově validovat. Tenhle úkol musí být první.

**Files:**
- Create: `tests/fixtures/reference/gtfs-old-api.zip`
- Modify: `.gitignore`

**Interfaces:**
- Produces: soubor `tests/fixtures/reference/gtfs-old-api.zip` — feed ze starého API, vstup pro Task 12.

- [ ] **Step 1: Ověř, že reference ještě existuje a je z doby před migrací**

```bash
ls -la data/gtfs.zip
unzip -p data/gtfs.zip stops.txt | head -3
unzip -p data/gtfs.zip trips.txt | wc -l
```

Očekávané: soubor z 8. 8. 2026, `stops.txt` začíná `S1P1,"Jesničánky,točna",50.015831,15.7719059,0,S1,1,0`. Když `data/gtfs.zip` chybí nebo byl přestavěn, **zastav a zeptej se** — bez reference nemá Task 12 co porovnávat.

- [ ] **Step 2: Zkopíruj ho mimo dosah buildu**

```bash
mkdir -p tests/fixtures/reference
cp data/gtfs.zip tests/fixtures/reference/gtfs-old-api.zip
```

- [ ] **Step 3: Ujisti se, že ho .gitignore nevyhodí**

```bash
git check-ignore -v tests/fixtures/reference/gtfs-old-api.zip || echo "není ignorován, dobře"
```

Když je ignorován (pravidlo na `*.zip`), přidej do `.gitignore` výjimku:

```
!tests/fixtures/reference/gtfs-old-api.zip
```

- [ ] **Step 4: Commit**

```bash
git add -f tests/fixtures/reference/gtfs-old-api.zip .gitignore
git commit -m "test: keep the last old-API feed as a cross-validation reference"
```

---

### Task 2: Rotující podpis a konfigurace

**Files:**
- Create: `src/dpmp_gtfs/protocol.py`, `tests/test_protocol.py`
- Modify: `src/dpmp_gtfs/config.py`

**Interfaces:**
- Produces:
  - `protocol.app_protocol(seed: str, now_ms: int | None = None) -> str`
  - `Settings.api_root: str`, `Settings.provider: str`, `Settings.protocol_seed: str`, `Settings.crawl_rate_limit: float`, `Settings.cis_dir: Path`, `Settings.cis_urls: tuple[str, ...]`

- [ ] **Step 1: Napiš padající test**

```python
# tests/test_protocol.py
from dpmp_gtfs.protocol import PROTOCOL_WINDOW_MS, app_protocol

SEED = "your-public-protocol-seed"


def test_signature_matches_the_web_app():
    # counter = 1000, i.e. unix_ms 900_000_000 .. 900_899_999
    assert app_protocol(SEED, now_ms=900_000_000) == app_protocol(SEED, now_ms=900_899_999)


def test_signature_rotates_every_fifteen_minutes():
    before = app_protocol(SEED, now_ms=900_000_000)
    after = app_protocol(SEED, now_ms=900_000_000 + PROTOCOL_WINDOW_MS)
    assert before != after


def test_signature_is_hex_sha256():
    sig = app_protocol(SEED, now_ms=900_000_000)
    assert len(sig) == 64
    assert set(sig) <= set("0123456789abcdef")
```

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dpmp_gtfs.protocol'`

- [ ] **Step 3: Implementuj**

```python
# src/dpmp_gtfs/protocol.py
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
```

- [ ] **Step 4: Spusť a ověř průchod**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -v`
Expected: PASS (3 testy)

- [ ] **Step 5: Přepiš nastavení**

V `src/dpmp_gtfs/config.py` nahraď blok `--- upstream API ---` (řádky 11–21) tímto a smaž `crawl_delay` (řádek 33–34):

```python
    # --- upstream API -------------------------------------------------------
    api_root: str = "https://api.mhdonline.cz"
    provider: str = "pardubice"
    """Path prefix the new API groups everything under. They also publish
    ``kromeriz`` and an aggregate ``global``; we only ever want Pardubice."""

    protocol_seed: str = "your-public-protocol-seed"
    """HMAC seed for the ``X-App-Protocol`` header -- see
    :mod:`dpmp_gtfs.protocol`. Public, but kept in settings so it can be
    swapped without a code change."""

    user_agent: str = "dpmp-to-gtfsr/0.1 (+https://github.com/xaralis/dpmp-to-gtfsr)"

    # --- CIS ----------------------------------------------------------------
    cis_urls: tuple[str, ...] = (
        "https://portal.cisjr.cz/pub/netex/NeTEx_DrahyMestske.zip",
        "https://portal.cisjr.cz/pub/netex/NeTEx_VerejnaLinkovaDoprava.zip",
    )
    """Trolleybus lines live in the first archive, buses in the second. Both
    are needed: JDF alone covers only 19 of the 32 lines."""

    cis_dir: Path = Path("data/cis")
```

A v bloku `--- politeness ---` nahraď `crawl_concurrency` a odstraněný `crawl_delay`:

```python
    crawl_concurrency: int = 8
    crawl_rate_limit: float = 8.0
    """Sustained requests per second. Expressed as a rate rather than as a
    sleep between requests: with N concurrent workers a fixed sleep makes the
    real rate depend on latency."""
```

- [ ] **Step 6: Ověř, že se nastavení načte a nic se nerozbilo jinde**

```bash
.venv/bin/python -c "from dpmp_gtfs.config import settings; print(settings.api_root, settings.provider, settings.crawl_rate_limit)"
grep -rn "crawl_delay\|api_key" src/ tests/ --include=*.py
```

Expected: vypíše `https://api.mhdonline.cz pardubice 8.0`. Grep najde zbylá použití v `api/client.py` — ta padnou v Tasku 3, teď je nech být.

- [ ] **Step 7: Commit**

```bash
git add src/dpmp_gtfs/protocol.py tests/test_protocol.py src/dpmp_gtfs/config.py
git commit -m "feat: rotating X-App-Protocol signature and mhdonline settings"
```

---

### Task 3: Klient nového API

**Files:**
- Modify: `src/dpmp_gtfs/api/client.py` (celý přepis)
- Test: `tests/test_client.py` (celý přepis)

**Interfaces:**
- Consumes: `protocol.app_protocol`, `Settings.api_root/provider/protocol_seed/crawl_rate_limit/crawl_concurrency`
- Produces: `DpmpApiClient` s metodami `stops() -> list[Stop]`, `lines() -> list[Line]`, `vehicles() -> list[Vehicle]`, `connection(line: str, number: int) -> Connection | None`, `events() -> list[dict[str, Any]]`. `connection()` vrací `None` na 404. Modely dodá Task 4 — psát v tomhle pořadí znamená, že Task 3 se dopíše až po něm; klidně implementuj Task 4 první a vrať se sem.

> **Poznámka pro implementátora:** Task 3 a Task 4 na sobě závisí oboustranně (klient importuje modely, modely nic z klienta). Udělej **nejdřív Task 4**, pak tenhle. Pořadí v dokumentu je dané čtivostí, ne závislostí.

- [ ] **Step 1: Napiš padající testy**

```python
# tests/test_client.py
import httpx
import pytest

from dpmp_gtfs.api import DpmpApiClient
from dpmp_gtfs.config import Settings
from dpmp_gtfs.exceptions import DpmpApiError

API = "https://api.mhdonline.cz"


def _settings(**over: object) -> Settings:
    return Settings(api_root=API, provider="pardubice", max_retries=2,
                    crawl_rate_limit=1000.0, **over)  # type: ignore[arg-type]


async def test_sends_the_protocol_header_and_uses_get(vehicles_payload):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=vehicles_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=API) as raw:
        async with DpmpApiClient(settings=_settings(), client=raw) as api:
            await api.vehicles()

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/pardubice/vehicles"
    assert len(seen[0].headers["X-App-Protocol"]) == 64


async def test_refreshes_the_signature_once_on_401(vehicles_payload):
    codes = [401, 200]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-App-Protocol"])
        code = codes.pop(0)
        return httpx.Response(code, json=vehicles_payload if code == 200 else {})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=API) as raw:
        async with DpmpApiClient(settings=_settings(), client=raw) as api:
            result = await api.vehicles()

    assert len(seen) == 2
    assert len(result) > 0


async def test_connection_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=API) as raw:
        async with DpmpApiClient(settings=_settings(), client=raw) as api:
            assert await api.connection("1", 999) is None


async def test_raises_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=API) as raw:
        async with DpmpApiClient(settings=_settings(), client=raw) as api:
            with pytest.raises(DpmpApiError):
                await api.vehicles()
```

Do `tests/conftest.py` přidej fixture (payload nahraješ v Tasku 4, prozatím stačí minimální tvar):

```python
@pytest.fixture
def vehicles_payload() -> dict[str, Any]:
    return load("vehicles.json")
```

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_client.py -v`
Expected: FAIL — `DpmpApiClient` nemá `vehicles()`, `connection()`.

- [ ] **Step 3: Přepiš klienta**

```python
# src/dpmp_gtfs/api/client.py
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
            vehicles = await api.vehicles()
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

    async def vehicles(self) -> list[Vehicle]:
        payload = VehiclesResponse.model_validate(await self._get("vehicles"))
        return payload.vehicles

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
```

- [ ] **Step 4: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_client.py tests/test_protocol.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dpmp_gtfs/api/client.py tests/test_client.py tests/conftest.py
git commit -m "feat: GET-based client for api.mhdonline.cz with per-request signing"
```

---

### Task 4: Modely nového API

**Files:**
- Modify: `src/dpmp_gtfs/api/models.py` (celý přepis)
- Test: `tests/test_models.py` (celý přepis)
- Create: `tests/fixtures/vehicles.json`, `tests/fixtures/stops.json`, `tests/fixtures/lines.json`, `tests/fixtures/connection-1-1.json`

**Interfaces:**
- Produces: `Vehicle`, `VehiclesResponse`, `Stop`, `Line`, `Connection`, `ConnectionStop`, `parse_iso_duration`

- [ ] **Step 1: Nahraj fixtures z živého API**

```bash
.venv/bin/python - <<'PY'
import hashlib, hmac, json, pathlib, time, httpx
seed = b"your-public-protocol-seed"
sig = hmac.new(seed, str(int(time.time()*1000)//900_000).encode(), hashlib.sha256).hexdigest()
h = {"X-App-Protocol": sig, "Accept": "application/json"}
out = pathlib.Path("tests/fixtures")
for name, path in [("vehicles", "vehicles"), ("stops", "stops"), ("lines", "lines"),
                   ("connection-1-1", "connections/1/1")]:
    r = httpx.get(f"https://api.mhdonline.cz/pardubice/{path}", headers=h, timeout=30)
    r.raise_for_status()
    (out / f"{name}.json").write_text(json.dumps(r.json(), ensure_ascii=False, indent=1), encoding="utf8")
    print(name, r.status_code)
PY
```

- [ ] **Step 2: Napiš padající testy**

```python
# tests/test_models.py
import datetime as dt

import pytest

from dpmp_gtfs.api.models import Connection, Line, Stop, VehiclesResponse, parse_iso_duration


def test_vehicles_parse(vehicles_payload):
    payload = VehiclesResponse.model_validate(vehicles_payload)
    assert payload.vehicles
    v = payload.vehicles[0]
    assert v.vid
    assert isinstance(v.gps_latitude, float)


def test_delay_is_a_real_signed_duration():
    assert parse_iso_duration("-PT1M43S") == dt.timedelta(seconds=-103)
    assert parse_iso_duration("PT2M") == dt.timedelta(minutes=2)
    assert parse_iso_duration("PT0S") == dt.timedelta(0)
    assert parse_iso_duration("-PT1H0M5S") == dt.timedelta(seconds=-3605)


def test_delay_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_iso_duration("1M43S")


def test_stop_flags_come_from_fixed_codes():
    step_free = Stop.model_validate(
        {"id": 1, "name": "Zkušební", "gpsLat": 50.0, "gpsLon": 15.0, "fixedCodes": ["@"]}
    )
    on_request = Stop.model_validate(
        {"id": 2, "name": "Druhá", "gpsLat": 50.0, "gpsLon": 15.0, "fixedCodes": ["x"]}
    )
    plain = Stop.model_validate({"id": 3, "name": "Třetí", "gpsLat": 50.0, "gpsLon": 15.0})

    assert step_free.step_free and not step_free.on_request
    # Lower-case x is "on request"; upper-case X on a *trip* means weekdays.
    assert on_request.on_request and not on_request.step_free
    assert not plain.step_free and not plain.on_request


def test_line_exposes_its_jdf_id(lines_payload):
    line = Line.model_validate(lines_payload[0])
    assert line.jdf_id.startswith("655")
    assert line.id


def test_connection_stop_times(connection_payload):
    conn = Connection.model_validate(connection_payload)
    assert conn.stops
    assert conn.stops[0].departure.hour >= 0
    assert conn.line_id
```

Do `tests/conftest.py` přidej `stops_payload`, `lines_payload`, `connection_payload` podle vzoru stávajících fixtures a **smaž** fixtures pro zaniklé endpointy (`buses_payload`, `stations_payload`, `codes_payload`, `detail_payload`, `connections_payload`).

- [ ] **Step 3: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_iso_duration'`

- [ ] **Step 4: Přepiš modely**

```python
# src/dpmp_gtfs/api/models.py
"""Typed models for the api.mhdonline.cz responses.

Everything the upstream does oddly is normalised here, once:

* ``currentDelay`` is an ISO-8601 duration string and is a **real** delay --
  unlike the old API's ``time_difference``, which counted down to the next
  scheduled departure and was not one.
* ``fixedCodes`` appear on both trips and stops, and the same letter means
  different things at each level. Case matters: ``X`` on a trip is "runs on
  weekdays", ``x`` on a stop is "request stop".
* ``lineId`` is a string, and ``jdfId`` is the CIS line number that joins this
  API to the timetable registry.
"""

import datetime as dt
import re

from pydantic import BaseModel, Field

_DURATION = re.compile(
    r"^(?P<sign>-?)P(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)
_HHMMSS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")

# Trip-level fixed codes (JDF convention).
WORKING_DAYS = "X"
SATURDAY = "6"
SUNDAY_AND_HOLIDAYS = "+"
LOW_FLOOR = "@"

# Stop-level fixed codes. Note the case clash with WORKING_DAYS above.
STEP_FREE_STOP = "@"
STOP_ON_REQUEST = "x"


def parse_iso_duration(value: str) -> dt.timedelta:
    """``"-PT1M43S"`` -> ``timedelta(seconds=-103)``.

    The sign applies to the whole magnitude, so a vehicle 103 seconds *early*
    reads as a negative delay -- which is exactly what GTFS-RT wants.
    """
    m = _DURATION.match(value)
    if not m or value in ("P", "PT", "-P", "-PT"):
        raise ValueError(f"not an ISO-8601 duration: {value!r}")
    magnitude = dt.timedelta(
        days=int(m.group("d") or 0),
        hours=int(m.group("h") or 0),
        minutes=int(m.group("m") or 0),
        seconds=float(m.group("s") or 0),
    )
    return -magnitude if m.group("sign") else magnitude


def parse_hhmmss(value: str) -> dt.time:
    """``"04:12:00"`` -> ``04:12:00``."""
    m = _HHMMSS.match(value)
    if not m:
        raise ValueError(f"not a HH:MM:SS time: {value!r}")
    return dt.time(int(m.group(1)), int(m.group(2)), int(m.group(3)))


# --- /{provider}/vehicles ---------------------------------------------------


class Vehicle(BaseModel):
    vid: str
    line_id: str = Field(alias="lineId")
    line_direction: str = Field(alias="lineDirection", default="")
    destination_name: str = Field(alias="destinationName", default="")
    last_stop_id: int | None = Field(alias="lastStopId", default=None)
    next_stop_id: int | None = Field(alias="nextStopId", default=None)
    next_stop_platform_id: int | None = Field(alias="nextStopPlatformId", default=None)
    next_stop_scheduled_departure: str | None = Field(
        alias="nextStopScheduledDepartureTime", default=None
    )
    gps_latitude: float = Field(alias="gpsLat")
    gps_longitude: float = Field(alias="gpsLon")
    current_delay: str | None = Field(alias="currentDelay", default=None)
    connection_id: int = Field(alias="connectionId")
    on_station: bool = Field(alias="onStation", default=False)

    model_config = {"populate_by_name": True}

    @property
    def delay(self) -> dt.timedelta | None:
        """The vehicle's delay, or ``None`` when the upstream reports none.

        Absent is not zero: publishing zero would assert punctuality for every
        vehicle the upstream declined to describe.
        """
        return parse_iso_duration(self.current_delay) if self.current_delay else None


class VehiclesResponse(BaseModel):
    time: dt.datetime
    vehicles: list[Vehicle] = Field(default_factory=list)


# --- /{provider}/stops and /{provider}/lines --------------------------------


class Stop(BaseModel):
    id: int
    name: str
    gps_latitude: float = Field(alias="gpsLat")
    gps_longitude: float = Field(alias="gpsLon")
    fixed_codes: list[str] = Field(alias="fixedCodes", default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def step_free(self) -> bool:
        return STEP_FREE_STOP in self.fixed_codes

    @property
    def on_request(self) -> bool:
        """"Zastávka na znamení" -- the vehicle only calls if asked."""
        return STOP_ON_REQUEST in self.fixed_codes


class Line(BaseModel):
    id: str
    jdf_id: str = Field(alias="jdfId")
    """The CIS line number, e.g. ``655001``. The only join between this API and
    the timetable registry."""
    enabled: bool = True

    model_config = {"populate_by_name": True}


# --- /{provider}/connections/{line}/{number} --------------------------------


class ConnectionStop(BaseModel):
    stop_id: int = Field(alias="stopId")
    platform_id: str = Field(alias="platformId", default="")
    departure_time: str | None = Field(alias="departureTime", default=None)
    arrival_time: str | None = Field(alias="arrivalTime", default=None)

    model_config = {"populate_by_name": True}

    @property
    def departure(self) -> dt.time:
        """The stop's timetable time.

        Only the terminal stop of a trip carries ``arrivalTime``; everywhere
        else ``departureTime`` is the authoritative one. The old API behaved
        the same way, and a model that demanded ``departureTime`` would raise
        on the last stop of every trip in the network.
        """
        raw = self.departure_time or self.arrival_time
        if raw is None:
            raise ValueError(f"stop {self.stop_id} has no time at all")
        return parse_hhmmss(raw)


class Connection(BaseModel):
    line_id: str = Field(alias="lineId")
    connection_id: int = Field(alias="connectionId")
    fixed_codes: list[str] = Field(alias="fixedCodes", default_factory=list)
    stops: list[ConnectionStop] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def low_floor(self) -> bool:
        return LOW_FLOOR in self.fixed_codes
```

- [ ] **Step 5: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dpmp_gtfs/api/models.py tests/test_models.py tests/conftest.py tests/fixtures/
git commit -m "feat: models for the mhdonline API, with real signed delays"
```

---

### Task 5: Stažení archivů CIS

**Files:**
- Create: `src/dpmp_gtfs/cis/__init__.py`, `src/dpmp_gtfs/cis/archive.py`, `tests/test_cis_archive.py`

**Interfaces:**
- Produces: `archive.fetch_archives(urls, dest, client=None) -> list[Path]`, `archive.CisUnavailable`

- [ ] **Step 1: Napiš padající testy**

```python
# tests/test_cis_archive.py
import httpx
import pytest

from dpmp_gtfs.cis.archive import CisUnavailable, fetch_archives

URL = "https://portal.cisjr.cz/pub/netex/Test.zip"


async def test_downloads_and_records_last_modified(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"zipbytes",
                              headers={"Last-Modified": "Fri, 07 Aug 2026 19:54:35 GMT"})

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
```

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_cis_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dpmp_gtfs.cis'`

- [ ] **Step 3: Implementuj**

```python
# src/dpmp_gtfs/cis/__init__.py
"""The CIS JŘ timetable registry: which trips exist, and which way they run.

CIS is the primary source DPMP files its timetables with. The API that used to
publish them (``online.dpmp.cz/api/connections``) is gone, and the replacement
has no bulk listing at all -- so this package supplies the one thing the API
can no longer answer.
"""

from .archive import CisUnavailable, fetch_archives

__all__ = ["CisUnavailable", "fetch_archives"]
```

Task 6 rozšíří `__all__` o `ServiceIndex` a `build_index`. Nezakládej `index.py` dopředu ani nenechávej zakomentovaný import — prázdný modul a mrtvý kód by prošly review jen proto, že to řekl plán.

```python
# src/dpmp_gtfs/cis/archive.py
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


class CisUnavailable(RuntimeError):
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
```

- [ ] **Step 4: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_cis_archive.py -v`
Expected: PASS (4 testy)

- [ ] **Step 5: Commit**

```bash
git add src/dpmp_gtfs/cis/ tests/test_cis_archive.py
git commit -m "feat: fetch CIS NeTEx archives with conditional GET and cache fallback"
```

---

### Task 6: Rejstřík spojů z NeTEx

Jádro migrace. Vybírá správnou verzi linky a čte směr.

**Files:**
- Create: `tests/fixtures/netex/line-655001-v1.xml`, `tests/fixtures/netex/line-655001-v2.xml`
- Modify: `src/dpmp_gtfs/cis/index.py`, `src/dpmp_gtfs/cis/__init__.py`
- Test: `tests/test_cis_index.py`

**Interfaces:**
- Consumes: `archive.fetch_archives`
- Produces:
  - `ServiceIndex` — `dataclass(frozen=True)` s `lines: dict[str, LineServices]`
  - `LineServices` — `dataclass(frozen=True)` s `jdf_id: str`, `valid_from: dt.date`, `trips: dict[int, int]` (číslo spoje -> `direction_id`)
  - `build_index(paths: Iterable[Path], on_date: dt.date, operator: str = "63217066") -> ServiceIndex`

- [ ] **Step 1: Přidej `defusedxml` a vyrob fixtures**

```bash
.venv/bin/python -m pip install defusedxml types-defusedxml
mkdir -p tests/fixtures/netex
```

Do `pyproject.toml` přidej `defusedxml` do `dependencies` a `types-defusedxml` do vývojové skupiny, ať `mypy --strict` vidí typy.

`tests/fixtures/netex/line-655001-v1.xml` (starší verze, tři spoje):

```xml
<?xml version="1.0" encoding="utf-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex" version="1">
  <dataObjects>
    <CompositeFrame id="CZ:cisjr_jdf:CompositeFrame:1" version="1">
      <frames>
        <ResourceFrame id="CZ:cisjr_jdf:ResourceFrame:1" version="1">
          <organisations>
            <Operator id="CZ:cisjr_jdf:Operator:63217066_1" version="1">
              <PublicCode>63217066</PublicCode>
              <LegalName>Dopravní podnik města Pardubic a.s.</LegalName>
            </Operator>
          </organisations>
        </ResourceFrame>
        <ServiceFrame id="CZ:cisjr_jdf:ServiceFrame:1" version="1">
          <lines>
            <Line id="CZ:cisjr_jdf:Line:655001_1" version="1">
              <ValidBetween>
                <FromDate>2026-01-01T00:00:00</FromDate>
                <ToDate>2030-12-31T00:00:00</ToDate>
              </ValidBetween>
              <PublicCode>655001</PublicCode>
            </Line>
          </lines>
          <journeyPatterns>
            <ServiceJourneyPattern id="CZ:cisjr_jdf:ServiceJourneyPattern:1_out" version="1">
              <DirectionRef version="1" ref="CZ:cisjr_jdf:Direction:out"/>
            </ServiceJourneyPattern>
            <ServiceJourneyPattern id="CZ:cisjr_jdf:ServiceJourneyPattern:2_in" version="1">
              <DirectionRef version="1" ref="CZ:cisjr_jdf:Direction:in"/>
            </ServiceJourneyPattern>
          </journeyPatterns>
        </ServiceFrame>
        <TimetableFrame id="CZ:cisjr_jdf:TimetableFrame:1" version="1">
          <vehicleJourneys>
            <ServiceJourney id="CZ:cisjr_jdf:ServiceJourney:655001_1_1" version="1">
              <Name>1</Name>
              <ServiceJourneyPatternRef version="1" ref="CZ:cisjr_jdf:ServiceJourneyPattern:1_out"/>
            </ServiceJourney>
            <ServiceJourney id="CZ:cisjr_jdf:ServiceJourney:655001_1_2" version="1">
              <Name>2</Name>
              <ServiceJourneyPatternRef version="1" ref="CZ:cisjr_jdf:ServiceJourneyPattern:2_in"/>
            </ServiceJourney>
            <ServiceJourney id="CZ:cisjr_jdf:ServiceJourney:655001_1_9" version="1">
              <Name>9</Name>
              <ServiceJourneyPatternRef version="1" ref="CZ:cisjr_jdf:ServiceJourneyPattern:1_out"/>
            </ServiceJourney>
          </vehicleJourneys>
        </TimetableFrame>
      </frames>
    </CompositeFrame>
  </dataObjects>
</PublicationDelivery>
```

`tests/fixtures/netex/line-655001-v2.xml` — zkopíruj v1 a proveď tři změny: `FromDate` na `2026-07-01T00:00:00`, smaž celý `ServiceJourney` se jménem `9`, a u jména `2` nech `2_in`. Výsledek je novější verze se **dvěma** spoji.

- [ ] **Step 2: Napiš padající testy**

```python
# tests/test_cis_index.py
import datetime as dt
import zipfile
from pathlib import Path

import pytest

from dpmp_gtfs.cis.index import build_index

FIXTURES = Path(__file__).parent / "fixtures" / "netex"


def _archive(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "netex.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.write(FIXTURES / name, arcname=name)
    return path


def test_later_valid_from_wins(tmp_path):
    archive = _archive(tmp_path, "line-655001-v1.xml", "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    line = index.lines["655001"]
    assert line.valid_from == dt.date(2026, 7, 1)
    # v1's third trip (9) must not leak in -- unioning versions would invent it.
    assert set(line.trips) == {1, 2}


def test_direction_comes_from_the_journey_pattern(tmp_path):
    archive = _archive(tmp_path, "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))

    trips = index.lines["655001"].trips
    assert trips[1] == 0  # _out
    assert trips[2] == 1  # _in


def test_versions_not_yet_valid_are_ignored(tmp_path):
    archive = _archive(tmp_path, "line-655001-v1.xml", "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 3, 1))

    line = index.lines["655001"]
    assert line.valid_from == dt.date(2026, 1, 1)
    assert set(line.trips) == {1, 2, 9}


def test_files_from_other_operators_are_skipped(tmp_path):
    other = tmp_path / "other.xml"
    other.write_text(
        (FIXTURES / "line-655001-v2.xml").read_text(encoding="utf8").replace("63217066", "11111111"),
        encoding="utf8",
    )
    path = tmp_path / "netex.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.write(other, arcname="other.xml")

    index = build_index([path], on_date=dt.date(2026, 8, 10))
    assert index.lines == {}


def test_unknown_line_raises_keyerror(tmp_path):
    archive = _archive(tmp_path, "line-655001-v2.xml")
    index = build_index([archive], on_date=dt.date(2026, 8, 10))
    with pytest.raises(KeyError):
        index.lines["655999"]
```

- [ ] **Step 3: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_cis_index.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_index'`

- [ ] **Step 4: Implementuj**

```python
# src/dpmp_gtfs/cis/index.py
"""Reads the NeTEx archives into "which trips exist, and which way they run".

Deliberately narrow. NeTEx can also describe stop times and calendars, but the
API answers those better -- it has platforms, which CIS does not -- and two
competing descriptions of the same trip would mean deciding which one wins on
every field. So nothing but trip numbers and directions crosses this boundary.

Version selection is the subtle part. A line is usually present several times
over, and more than one version is typically valid *today*: line 655001 ships
as a 283-trip version valid from 2026-01-01 and a 206-trip version valid from
2026-07-01, and the API agrees with the latter exactly. Unioning them would
invent 77 trips that do not run, so the rule is the latest ``FromDate`` that
has already started -- not merely one whose window covers the date.
"""

import datetime as dt
import logging
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring

logger = logging.getLogger(__name__)

NS = {"n": "http://www.netex.org.uk/netex"}
DPMP_OPERATOR = "63217066"

_LINE_NUMBER = re.compile(r":Line:(\d+)")
_PATTERN_KEY = re.compile(r":ServiceJourneyPattern:(.+)$")

OUTBOUND = 0
INBOUND = 1


@dataclass(frozen=True, slots=True)
class LineServices:
    """One line's trips, as of a date."""

    jdf_id: str
    valid_from: dt.date
    trips: dict[int, int]
    """Trip number -> direction_id. The trip number is the JDF "spoj", which
    the API reports as ``connectionId`` -- the join between the two sources."""


@dataclass(frozen=True, slots=True)
class ServiceIndex:
    lines: dict[str, LineServices]
    """Keyed by JDF line number, e.g. ``"655001"``."""

    @property
    def trip_count(self) -> int:
        return sum(len(line.trips) for line in self.lines.values())


def build_index(
    paths: Iterable[Path],
    on_date: dt.date,
    operator: str = DPMP_OPERATOR,
) -> ServiceIndex:
    """Read every archive and keep, per line, the version in force on ``on_date``."""
    best: dict[str, LineServices] = {}
    needle = operator.encode()

    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                blob = archive.read(name)
                # Cheap prefilter: ~36 of 1,043 files are DPMP's, and parsing
                # the rest would cost seconds of XML for nothing.
                if needle not in blob:
                    continue
                parsed = _parse(blob, operator)
                if parsed is None:
                    continue
                if parsed.valid_from > on_date:
                    continue
                current = best.get(parsed.jdf_id)
                if current is None or parsed.valid_from > current.valid_from:
                    best[parsed.jdf_id] = parsed

    logger.info(
        "CIS index: %d lines, %d trips as of %s",
        len(best),
        sum(len(line.trips) for line in best.values()),
        on_date,
    )
    return ServiceIndex(lines=best)


def _parse(blob: bytes, operator: str) -> LineServices | None:
    # defusedxml, not the stdlib parser: this is bulk third-party XML parsed
    # unattended, and entity-expansion attacks are exactly what it is exposed
    # to. Returns an ordinary ElementTree Element, so nothing else changes.
    root = fromstring(blob)

    codes = [e.text for e in root.iterfind(".//n:Operator/n:PublicCode", NS)]
    if operator not in [c for c in codes if c]:
        return None

    line = root.find(".//n:Line", NS)
    if line is None:
        return None
    jdf_id = _line_number(line.get("id") or "")
    valid_from = _valid_from(line)
    if jdf_id is None or valid_from is None:
        return None

    directions = _pattern_directions(root)

    trips: dict[int, int] = {}
    for journey in root.iterfind(".//n:ServiceJourney", NS):
        name = journey.findtext("n:Name", namespaces=NS)
        ref = journey.find("n:ServiceJourneyPatternRef", NS)
        if name is None or not name.isdigit() or ref is None:
            continue
        key = _pattern_key(ref.get("ref") or "")
        trips[int(name)] = directions.get(key, OUTBOUND)

    if not trips:
        return None
    return LineServices(jdf_id=jdf_id, valid_from=valid_from, trips=trips)


def _pattern_directions(root: Element) -> dict[str, int]:
    """``{"1_out": 0, "2_in": 1}`` -- the direction each journey pattern runs.

    The old API gave each stop an ``index`` into the line's canonical ordering
    and direction was read off whether a trip walked it up or down. That field
    is gone; this is its replacement, and it is a stated direction rather than
    an inferred one.
    """
    out: dict[str, int] = {}
    for pattern in root.iterfind(".//n:ServiceJourneyPattern", NS):
        key = _pattern_key(pattern.get("id") or "")
        if key is None:
            continue
        ref = pattern.find("n:DirectionRef", NS)
        target = (ref.get("ref") or "") if ref is not None else ""
        out[key] = INBOUND if target.endswith(":in") else OUTBOUND
    return out


def _pattern_key(ref: str) -> str | None:
    m = _PATTERN_KEY.search(ref)
    return m.group(1) if m else None


def _line_number(ref: str) -> str | None:
    m = _LINE_NUMBER.search(ref)
    return m.group(1) if m else None


def _valid_from(line: Element) -> dt.date | None:
    raw = line.findtext("n:ValidBetween/n:FromDate", namespaces=NS)
    if not raw:
        return None
    return dt.datetime.fromisoformat(raw).date()
```

Rozšiř `src/dpmp_gtfs/cis/__init__.py` o nová jména:

```python
from .archive import CisUnavailable, fetch_archives
from .index import ServiceIndex, build_index

__all__ = ["CisUnavailable", "ServiceIndex", "build_index", "fetch_archives"]
```

- [ ] **Step 5: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_cis_index.py -v`
Expected: PASS (5 testů)

- [ ] **Step 6: Ověř proti skutečnému CIS**

Tenhle krok se **necommituje jako test** (sahá na síť), je to jednorázová kontrola, že fixtures nelžou:

```bash
.venv/bin/python - <<'PY'
import asyncio, datetime as dt
from pathlib import Path
from dpmp_gtfs.cis import build_index, fetch_archives
from dpmp_gtfs.config import settings

paths = asyncio.run(fetch_archives(settings.cis_urls, Path("data/cis")))
idx = build_index(paths, on_date=dt.date.today())
print("linek:", len(idx.lines), "spojů:", idx.trip_count)
l1 = idx.lines["655001"]
print("655001 valid_from:", l1.valid_from, "spojů:", len(l1.trips))
print("směry:", sorted(set(l1.trips.values())))
PY
```

Expected: `655001` má `valid_from` 2026-07-01 a **206** spojů. Když jich je 283 nebo 347, výběr verze je špatně — vrať se ke Step 4.

- [ ] **Step 7: Commit**

```bash
git add src/dpmp_gtfs/cis/index.py src/dpmp_gtfs/cis/__init__.py tests/test_cis_index.py tests/fixtures/netex/
git commit -m "feat: CIS service index with version selection and direction"
```

---

### Task 7: Crawler proti rejstříku

**Files:**
- Modify: `src/dpmp_gtfs/static/crawler.py` (celý přepis), `src/dpmp_gtfs/types.py:45-58`
- Test: `tests/test_crawler.py` (celý přepis)

**Interfaces:**
- Consumes: `DpmpApiClient.stops/lines/connection`, `ServiceIndex`
- Produces:
  - `Timetable` — `stops: list[Stop]`, `lines: list[Line]`, `directions: dict[tuple[str, int], int]`, `connections: dict[tuple[str, int], Connection]`
  - `crawler.crawl(api, index, attempts=3, backoff=30.0) -> Timetable`
  - `crawler.MISSING_TRIP_LIMIT = 0.05`

- [ ] **Step 1: Uprav `Timetable` v `types.py`**

Nahraď třídu `Timetable` (řádky 45–58) a uprav import na řádku 12:

```python
from dpmp_gtfs.api.models import Connection, Line, Stop as ApiStop
```

```python
@dataclass(slots=True)
class Timetable:
    """Everything needed to build a static feed.

    Assembled from two sources: the CIS registry says which trips exist and
    which way they run, the API says what each one does.
    """

    stops: list[ApiStop]
    lines: list[Line]
    directions: dict[tuple[str, int], int] = field(default_factory=dict)
    """``(line_id, connection_id)`` -> ``direction_id``, from CIS."""
    connections: dict[tuple[str, int], Connection] = field(default_factory=dict)
    """``(line_id, connection_id)`` -> the trip's stop times, from the API."""

    @property
    def trip_count(self) -> int:
        return len(self.connections)
```

- [ ] **Step 2: Napiš padající testy**

```python
# tests/test_crawler.py
import datetime as dt

import pytest

from dpmp_gtfs.cis.index import LineServices, ServiceIndex
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.static.crawler import crawl


class FakeApi:
    def __init__(self, present: set[tuple[str, int]]):
        self.present = present
        self.asked: list[tuple[str, int]] = []

    async def stops(self):
        from dpmp_gtfs.api.models import Stop
        return [Stop.model_validate({"id": 1, "name": "A", "gpsLat": 50.0, "gpsLon": 15.0})]

    async def lines(self):
        from dpmp_gtfs.api.models import Line
        return [Line.model_validate({"id": "1", "jdfId": "655001", "enabled": True})]

    async def connection(self, line: str, number: int):
        from dpmp_gtfs.api.models import Connection
        self.asked.append((line, number))
        if (line, number) not in self.present:
            return None
        return Connection.model_validate({
            "lineId": line, "connectionId": number, "fixedCodes": ["X"],
            "stops": [{"stopId": 1, "platformId": "1", "departureTime": "04:12:00"}],
        })


def _index(trips: dict[int, int]) -> ServiceIndex:
    return ServiceIndex(lines={"655001": LineServices("655001", dt.date(2026, 7, 1), trips)})


async def test_asks_only_for_trips_the_registry_lists():
    api = FakeApi(present={("1", 1), ("1", 3)})
    table = await crawl(api, _index({1: 0, 3: 1}))

    assert sorted(api.asked) == [("1", 1), ("1", 3)]
    assert table.trip_count == 2
    assert table.directions[("1", 3)] == 1


async def test_a_few_missing_trips_are_skipped():
    trips = {n: 0 for n in range(1, 41)}
    api = FakeApi(present={("1", n) for n in range(1, 41)} - {("1", 7)})
    table = await crawl(api, _index(trips))

    assert table.trip_count == 39


async def test_too_many_missing_trips_fails_the_build():
    trips = {n: 0 for n in range(1, 41)}
    api = FakeApi(present={("1", n) for n in range(1, 31)})  # 25% missing
    with pytest.raises(DpmpApiError, match="655001"):
        await crawl(api, _index(trips), attempts=1)


async def test_lines_the_registry_does_not_know_are_skipped():
    api = FakeApi(present=set())
    table = await crawl(api, ServiceIndex(lines={}))
    assert table.trip_count == 0
    assert api.asked == []
```

- [ ] **Step 3: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_crawler.py -v`
Expected: FAIL — `crawl()` má jinou signaturu.

- [ ] **Step 4: Přepiš crawler**

```python
# src/dpmp_gtfs/static/crawler.py
"""Fetches the timetable, guided by the CIS registry.

The API exposes trips one at a time and no longer lists them, so the registry
supplies the list and this module fetches exactly those -- roughly 2,700
requests at 8/s, about six minutes.

The two sources drift: CIS republishes in batches, the API changes when DPMP
changes it. A trip the registry lists and the API answers 404 for is skipped,
because a genuinely cancelled trip looks exactly like that. Too many of them
on one line is a different thing entirely -- it means the wrong version was
selected -- so that fails the build rather than quietly halving a line.
"""

import asyncio
import logging
from typing import Protocol

from dpmp_gtfs.api.models import Connection, Line, Stop
from dpmp_gtfs.cis.index import ServiceIndex
from dpmp_gtfs.exceptions import DpmpApiError
from dpmp_gtfs.types import Timetable

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 30.0
"""Seconds before the first retry, doubling after that. Generous on purpose:
upstream outages last minutes, and a crawl is a nightly job with nobody
waiting on it."""

MISSING_TRIP_LIMIT = 0.05
"""How much of one line's registry may be missing from the API before the
build is treated as wrong rather than merely out of date."""


class SupportsTimetable(Protocol):
    async def stops(self) -> list[Stop]: ...
    async def lines(self) -> list[Line]: ...
    async def connection(self, line: str, number: int) -> Connection | None: ...


async def crawl(
    api: SupportsTimetable,
    index: ServiceIndex,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
) -> Timetable:
    """Fetch the whole timetable, retrying the crawl as a whole.

    The client already retries individual requests, but that is not enough: a
    crawl is thousands of them spread over minutes, so an outage that outlasts
    one request's retries throws away the entire run.

    Raises :class:`DpmpApiError` once attempts are exhausted rather than
    returning what it managed to collect -- to a consumer, a missing trip is
    indistinguishable from a cancelled one.
    """
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await _crawl_once(api, index)
        except DpmpApiError as exc:
            last = exc
            if attempt == attempts:
                break
            delay = backoff * 2 ** (attempt - 1)
            logger.warning(
                "crawl attempt %d/%d failed (%s), retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    raise DpmpApiError(f"crawl failed after {attempts} attempts: {last!r}") from last


async def _crawl_once(api: SupportsTimetable, index: ServiceIndex) -> Timetable:
    stops, lines = await asyncio.gather(api.stops(), api.lines())
    logger.info("crawling %d lines, %d stops", len(lines), len(stops))

    timetable = Timetable(stops=stops, lines=lines)

    for line in lines:
        services = index.lines.get(line.jdf_id)
        if services is None:
            logger.warning(
                "line %s (%s) is not in the CIS registry; it will have no trips",
                line.id,
                line.jdf_id,
            )
            continue

        wanted = sorted(services.trips)
        results = await asyncio.gather(*(api.connection(line.id, n) for n in wanted))

        missing = 0
        for number, connection in zip(wanted, results, strict=True):
            if connection is None:
                missing += 1
                logger.debug("line %s trip %d is in CIS but not in the API", line.id, number)
                continue
            timetable.connections[(line.id, number)] = connection
            timetable.directions[(line.id, number)] = services.trips[number]

        if wanted and missing / len(wanted) > MISSING_TRIP_LIMIT:
            raise DpmpApiError(
                f"line {line.id} ({line.jdf_id}): {missing} of {len(wanted)} registry trips "
                f"are absent from the API -- the CIS version in force is probably not "
                f"{services.valid_from}"
            )
        if missing:
            logger.info("line %s: skipped %d of %d trips", line.id, missing, len(wanted))

    logger.info("crawl complete: %d trips", timetable.trip_count)
    return timetable
```

- [ ] **Step 5: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_crawler.py -v`
Expected: PASS (4 testy)

- [ ] **Step 6: Commit**

```bash
git add src/dpmp_gtfs/static/crawler.py src/dpmp_gtfs/types.py tests/test_crawler.py
git commit -m "feat: crawl exactly the trips the CIS registry lists"
```

---

### Task 8: Kalendář z písmen JDF

**Files:**
- Modify: `src/dpmp_gtfs/static/calendar.py:1-32`
- Test: `tests/test_calendar.py`

**Interfaces:**
- Consumes: `models.WORKING_DAYS/SATURDAY/SUNDAY_AND_HOLIDAYS`
- Produces: `calendar.service_from_codes(codes: Iterable[str]) -> Service` (stejné jméno, jiný typ prvku)

- [ ] **Step 1: Napiš padající testy**

Nahraď v `tests/test_calendar.py` testy, které volají `service_from_codes` s čísly, těmito:

```python
from dpmp_gtfs.static.calendar import service_from_codes


def test_weekday_trips():
    service = service_from_codes(["X", "@"])
    assert service.working_days and not service.saturday and not service.sunday
    assert service.service_id == "wd"


def test_weekend_trips():
    service = service_from_codes(["6", "+", "@"])
    assert not service.working_days and service.saturday and service.sunday
    assert service.service_id == "sa-su"


def test_low_floor_is_not_a_calendar_code():
    # "@" alone would leave a service running on no days at all.
    with pytest.raises(ValueError):
        service_from_codes(["@"]).service_id


def test_upper_and_lower_x_are_different_codes():
    # "x" is a stop-level "on request" marker and must never mean weekdays.
    assert service_from_codes(["X"]).working_days
    assert not service_from_codes(["x"]).working_days
```

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_calendar.py -v`
Expected: FAIL — `TypeError`/`AssertionError`, `WORKING_DAY` je int.

- [ ] **Step 3: Uprav kalendář**

Nahraď v `src/dpmp_gtfs/static/calendar.py` docstring modulu, import na řádku 17 a funkci `service_from_codes`:

```python
"""Service calendars, derived from each trip's JDF fixed codes.

The upstream has no notion of a service calendar: a trip carries a set of
one-character codes inherited from JDF. The old API published their meanings
at ``/api/codes``; the new one does not, so the mapping is spelled out here
against the JDF convention.

Case is significant. Upper-case ``X`` on a trip means "runs on weekdays";
lower-case ``x`` is a *stop*-level marker meaning "request stop" and must
never be read as a calendar code.

Across the whole network only a handful of distinct service patterns occur, so
the generated ``calendar.txt`` stays small and legible.
"""
```

```python
from dpmp_gtfs.api.models import SATURDAY, SUNDAY_AND_HOLIDAYS, WORKING_DAYS
```

```python
def service_from_codes(codes: Iterable[str]) -> Service:
    """Read a trip's fixed codes as the days it runs.

    Codes that describe the vehicle rather than the calendar (``@`` for a
    low-floor trip) are simply not matched here; a trip carrying only those
    yields a service that runs on no days, and ``Service.service_id`` raises
    rather than emitting an empty calendar entry.
    """
    present = set(codes)
    return Service(
        working_days=WORKING_DAYS in present,
        saturday=SATURDAY in present,
        sunday=SUNDAY_AND_HOLIDAYS in present,
    )
```

- [ ] **Step 4: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_calendar.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dpmp_gtfs/static/calendar.py tests/test_calendar.py
git commit -m "feat: read service calendars from JDF letter codes"
```

---

### Task 9: Builder a úklid upstream.py

**Files:**
- Modify: `src/dpmp_gtfs/static/builder.py`, `src/dpmp_gtfs/upstream.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `Timetable` (Task 7), `service_from_codes` (Task 8), `Stop.step_free/on_request`, `Connection.low_floor`
- Produces: beze změny — `build_feed`, `build_stops`, `build_routes`, `build_trips_and_stop_times`

- [ ] **Step 1: Osekej `upstream.py`**

Smaž `Coordinates`, `STATION_COORDINATES`, `STATION_NAMES`, `unused_overrides()`, konstanty `STOP_ON_REQUEST`, `LOW_FLOOR`, `STEP_FREE_STOP`, `WORKING_DAY`, `SATURDAY`, `SUNDAY_AND_HOLIDAYS` (přesunuly se do `api/models.py`) a `whole_number()`, pokud po Tasku 11 nikdo nevolá. Ponech `TROLLEYBUS_LINES` a přepiš registr v docstringu:

```python
"""What DPMP's data gets wrong, and what this project does about it.

Every correction applied to the upstream lives here. Each one is a standing
claim that the source data is wrong or incomplete, and each is a liability: if
DPMP fixes the underlying problem, the correction becomes a lie that quietly
outlives it.

The register
------------

**Neither source says which lines are trolleybus.** ``route_type`` has no
field anywhere in the API, so the split is carried as a constant below. The
CIS migration confirmed it independently rather than replacing it: the
thirteen lines published in ``NeTEx_DrahyMestske.zip`` are exactly the thirteen
listed here.

**Platform coordinates no longer exist upstream.** ``/stops`` publishes one
point per stop and there is no platform endpoint; CIS has no ``Quay`` elements
at all. Platforms therefore inherit their parent's position. Their numbers are
still real -- ``/connections`` reports them per stop -- so ``platform_code``
and the ``S{station}P{platform}`` ids survive; only the geometry is coarser.

Retired corrections
-------------------

The old API omitted stations 250, 252 and 253 and several platforms, which
this module backfilled from OpenStreetMap. ``api.mhdonline.cz`` publishes all
of them (Svítkov,západ 250; Vápenka 252; Mikulovice 178, aliased
"Mikulovice,škola"), within about 3 m and 26 m of the hand-sourced positions,
so the table is gone rather than left to drift behind the real values.
"""
```

- [ ] **Step 2: Napiš padající testy**

Přidej do `tests/test_builder.py`:

```python
def test_platforms_inherit_the_station_position(simple_timetable):
    stops = build_stops(simple_timetable)
    parent = next(s for s in stops if s.stop_id == "S1")
    child = next(s for s in stops if s.stop_id == "S1P1")

    assert (child.stop_lat, child.stop_lon) == (parent.stop_lat, parent.stop_lon)
    assert child.platform_code == "1"
    assert child.parent_station == "S1"


def test_wheelchair_boarding_comes_from_the_stop_fixed_codes(simple_timetable):
    stops = build_stops(simple_timetable)
    assert next(s for s in stops if s.stop_id == "S1").wheelchair_boarding == 1


def test_direction_comes_from_the_timetable_not_the_stop_order(simple_timetable):
    trips, _, _ = build_trips_and_stop_times(simple_timetable)
    assert {t.trip_id: t.direction_id for t in trips} == {"L1C1": 0, "L1C2": 1}
```

A fixture, kterou tyhle testy potřebují, do `tests/conftest.py`:

```python
@pytest.fixture
def simple_timetable():
    from dpmp_gtfs.api.models import Connection, Line, Stop
    from dpmp_gtfs.types import Timetable

    stops = [
        Stop.model_validate(
            {"id": 1, "name": "První", "gpsLat": 50.01, "gpsLon": 15.77, "fixedCodes": ["@"]}
        ),
        Stop.model_validate({"id": 2, "name": "Druhá", "gpsLat": 50.02, "gpsLon": 15.78}),
    ]
    lines = [Line.model_validate({"id": "1", "jdfId": "655001", "enabled": True})]

    def connection(number: int, first: int, second: int) -> Connection:
        return Connection.model_validate(
            {
                "lineId": "1",
                "connectionId": number,
                "fixedCodes": ["X", "@"],
                "stops": [
                    {"stopId": first, "platformId": "1", "departureTime": "04:12:00"},
                    {"stopId": second, "platformId": "2", "departureTime": "04:20:00"},
                ],
            }
        )

    return Timetable(
        stops=stops,
        lines=lines,
        directions={("1", 1): 0, ("1", 2): 1},
        connections={("1", 1): connection(1, 1, 2), ("1", 2): connection(2, 2, 1)},
    )
```

- [ ] **Step 3: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_builder.py -v`
Expected: FAIL — `build_stops` čte `timetable.stations`.

- [ ] **Step 4: Uprav builder**

Proveď v `src/dpmp_gtfs/static/builder.py` tyhle změny:

1. **Importy (řádky 25–30)** — nahraď blok z `upstream` jediným `from dpmp_gtfs.upstream import TROLLEYBUS_LINES` a přidej `from dpmp_gtfs.api.models import Connection`.

2. **`build_stops`** — přepiš tělo. Zdrojem jsou `timetable.stops` a `used_platforms`, coordinate overrides mizí:

```python
def build_stops(timetable: Timetable) -> list[Stop]:
    """One parent station per stop plus one child per platform in service.

    Timetables and realtime both address platforms, so the children carry the
    actual service; the parent exists so consumers can group them and so that
    transfers between platforms are understood.

    Platforms inherit the parent's position: no upstream publishes per-platform
    coordinates any more (see :mod:`dpmp_gtfs.upstream`). Their numbers are
    real, so the ids and ``platform_code`` are unchanged -- only the geometry
    is coarser than it used to be.
    """
    stops: list[Stop] = []
    used = used_platforms(timetable)

    for api_stop in timetable.stops:
        step_free = int(api_stop.step_free)
        parent = station_id(api_stop.id)
        stops.append(
            Stop(
                stop_id=parent,
                stop_name=api_stop.name,
                stop_lat=api_stop.gps_latitude,
                stop_lon=api_stop.gps_longitude,
                location_type=1,
                parent_station="",
                platform_code="",
                wheelchair_boarding=step_free,
            )
        )
        for platform in sorted(used.get(api_stop.id, set())):
            stops.append(
                Stop(
                    stop_id=stop_id(api_stop.id, platform),
                    stop_name=api_stop.name,
                    stop_lat=api_stop.gps_latitude,
                    stop_lon=api_stop.gps_longitude,
                    location_type=0,
                    parent_station=parent,
                    platform_code=str(platform),
                    wheelchair_boarding=step_free,
                )
            )

    return stops
```

3. **`used_platforms`** — čte z `connections`, `platformId` je řetězec:

```python
def used_platforms(timetable: Timetable) -> dict[int, set[int]]:
    """``{stop: {platform, ...}}`` as actually referenced by timetables.

    The timetable is the authority on which platforms exist; ``/stops`` does
    not describe them at all.
    """
    used: dict[int, set[int]] = {}
    for connection in timetable.connections.values():
        for stop in connection.stops:
            if stop.platform_id.isdigit():
                used.setdefault(stop.stop_id, set()).add(int(stop.platform_id))
    return used
```

4. **`build_routes`** — `/lines` už nenese zastávky, koncové stanice se berou z nejdelšího spoje linky:

```python
def build_routes(timetable: Timetable) -> list[Route]:
    """Routes, with terminals taken from each line's longest trip.

    ``/lines`` returns only ``{id, jdfId, enabled}`` -- the stop list the old
    API published alongside it is gone -- so the longest trip stands in for the
    line's shape. It is already fetched, so this costs nothing.
    """
    names = {s.id: s.name for s in timetable.stops}
    longest: dict[str, Connection] = {}
    for (line_id, _), connection in timetable.connections.items():
        current = longest.get(line_id)
        if current is None or len(connection.stops) > len(current.stops):
            longest[line_id] = connection

    routes: list[Route] = []
    for line in timetable.lines:
        terminals = ""
        if (best := longest.get(line.id)) and len(best.stops) >= 2:
            first = names.get(best.stops[0].stop_id, "")
            last = names.get(best.stops[-1].stop_id, "")
            terminals = f"{first} - {last}"
        number = int(line.id) if line.id.isdigit() else 0
        routes.append(
            Route(
                route_id=route_id(line.id),
                route_short_name=line.id,
                route_long_name=terminals,
                route_type=(
                    ROUTE_TYPE_TROLLEYBUS if number in TROLLEYBUS_LINES else ROUTE_TYPE_BUS
                ),
            )
        )
    return routes
```

5. **`build_trips_and_stop_times`** — klíčem je `(line_id, connection_id)`, kódy jsou písmena, směr z `timetable.directions`, `on_request` ze zastávky:

```python
    names = {s.id: s.name for s in timetable.stops}
    on_request_stops = {s.id for s in timetable.stops if s.on_request}

    for key, connection in sorted(timetable.connections.items()):
        line_id, connection_number = key
        if not connection.stops:
            logger.warning("trip %s/%s has no stops, skipping", line_id, connection_number)
            continue

        service = service_from_codes(connection.fixed_codes)
        services.setdefault(service.service_id, service)

        tid = trip_id(line_id, connection_number)
        trips.append(
            Trip(
                route_id=route_id(line_id),
                service_id=service.service_id,
                trip_id=tid,
                trip_headsign=names.get(connection.stops[-1].stop_id, ""),
                direction_id=timetable.directions.get(key, 0),
                wheelchair_accessible=1 if connection.low_floor else 0,
            )
        )
```

a uvnitř smyčky přes zastávky nahraď `on_request = STOP_ON_REQUEST in stop.codes` za `on_request = stop.stop_id in on_request_stops`, a `stop_id(...)` volej jako `stop_id(stop.stop_id, int(stop.platform_id))`.

6. **`direction_of`** a `stop_seconds` — `direction_of` smaž celou (nahradil ji rejstřík). `stop_seconds` přepiš na `list[ConnectionStop]` a `stop.departure` místo `stop.time`.

7. **`ids.py`** — `route_id` a `trip_id` teď dostávají `line_id: str`. Uprav anotace na `str` a ponech tělo (`f"L{line}"`, `f"L{line}C{connection}"`), aby se výstup nezměnil.

- [ ] **Step 5: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_builder.py tests/test_calendar.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dpmp_gtfs/static/builder.py src/dpmp_gtfs/upstream.py src/dpmp_gtfs/ids.py tests/test_builder.py
git commit -m "feat: build the static feed from mhdonline stops and the CIS registry"
```

---

### Task 10: Realtime bez trackeru

**Files:**
- Delete: `src/dpmp_gtfs/realtime/tracker.py`, `tests/test_tracker.py`
- Modify: `src/dpmp_gtfs/realtime/feed.py`, `src/dpmp_gtfs/realtime/index.py`
- Test: `tests/test_rt_feed.py`

**Interfaces:**
- Consumes: `Vehicle.delay`, `Vehicle.on_station`, `StaticIndex.lookup`
- Produces: `build_feed_message(vehicles: list[Vehicle], index: StaticIndex, now: dt.datetime | None = None) -> gtfsr.FeedMessage`

- [ ] **Step 1: Napiš padající testy**

```python
# tests/test_rt_feed.py -- přidej
def test_delay_comes_straight_from_the_vehicle(static_index):
    vehicle = _vehicle(current_delay="-PT1M43S")
    message = build_feed_message([vehicle], static_index)

    update = next(e.trip_update for e in message.entity if e.HasField("trip_update"))
    assert update.delay == -103


def test_no_delay_means_no_trip_update(static_index):
    vehicle = _vehicle(current_delay=None)
    message = build_feed_message([vehicle], static_index)

    assert not any(e.HasField("trip_update") for e in message.entity)
    assert any(e.HasField("vehicle") for e in message.entity)


def test_on_station_reports_stopped_at(static_index):
    from google.transit import gtfs_realtime_pb2 as gtfsr

    stopped = build_feed_message([_vehicle(on_station=True)], static_index)
    moving = build_feed_message([_vehicle(on_station=False)], static_index)

    assert next(e.vehicle for e in stopped.entity if e.HasField("vehicle")).current_status == \
        gtfsr.VehiclePosition.STOPPED_AT
    assert next(e.vehicle for e in moving.entity if e.HasField("vehicle")).current_status == \
        gtfsr.VehiclePosition.IN_TRANSIT_TO
```

Helper, který ty testy používají, do téhož souboru:

```python
def _vehicle(**over: object):
    from dpmp_gtfs.api.models import Vehicle

    payload: dict[str, object] = {
        "vid": "100",
        "lineId": "1",
        "connectionId": 1,
        "gpsLat": 50.01,
        "gpsLon": 15.77,
        "currentDelay": "PT0S",
        "onStation": False,
    }
    payload.update(over)
    return Vehicle.model_validate(payload)
```

Fixture `static_index` musí odpovídat spoji `("1", 1)`, aby `index.lookup` našel `L1C1`; postav ji ze stejného `simple_timetable` jako v Tasku 9, přes tutéž cestu, kterou `StaticIndex` staví scheduler.

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_rt_feed.py -v`
Expected: FAIL — `build_feed_message` čeká `tracker`.

- [ ] **Step 3: Uprav feed a smaž tracker**

V `src/dpmp_gtfs/realtime/feed.py`:

- docstring modulu na `"""Builds the GTFS-Realtime FeedMessage from a /{provider}/vehicles snapshot."""`
- smaž `from .tracker import DelayTracker, project_delay` a přesuň `project_delay` sem (je to čistá funkce nad `timedelta`, jinde se nepoužívá):

```python
DELAY_DECAY_PER_STOP = 0.9
"""How much of a delay is assumed to survive each further stop.

A vehicle that is late tends to catch up a little at every stop, so projecting
the measured delay unchanged to the end of the trip overstates it. This is a
smoothing assumption, not a measurement.
"""


def project_delay(delay: dt.timedelta, stops_ahead: int) -> int:
    """The delay expected ``stops_ahead`` stops from now, in whole seconds."""
    return int(delay.total_seconds() * DELAY_DECAY_PER_STOP**stops_ahead)
```

- signatura: `def build_feed_message(vehicles: list[Vehicle], index: StaticIndex, now: dt.datetime | None = None) -> gtfsr.FeedMessage:`
- `bus.line` -> `vehicle.line_id`, `bus.connection_no` -> `vehicle.connection_id`, `bus.state_dtime` -> `now` (nové API dává čas na úrovni odpovědi, ne vozidla; předej `VehiclesResponse.time` volajícím jako `now`)
- `bus.current_stop` -> `vehicle.next_stop_id` složené přes `stop_id(vehicle.next_stop_id, vehicle.next_stop_platform_id)` když je nástupiště známé, jinak `station_id(vehicle.next_stop_id)`
- `gps_course` blok smaž — nové API kurz nemá o nic víc než staré
- `current_status`:

```python
        current_status=(
            gtfsr.VehiclePosition.STOPPED_AT
            if vehicle.on_station
            else gtfsr.VehiclePosition.IN_TRANSIT_TO
        ),
```

- zpoždění:

```python
        # --- trip update ---
        delay = vehicle.delay
        if delay is None:
            # No evidence either way. Emitting zero here would assert
            # punctuality for every vehicle the upstream declined to describe.
            continue
```

a dál `delay=int(delay.total_seconds())`, v predikcích `project_delay(delay, offset)`.

```bash
git rm src/dpmp_gtfs/realtime/tracker.py tests/test_tracker.py
```

- [ ] **Step 4: Spusť testy**

Run: `.venv/bin/python -m pytest tests/test_rt_feed.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/dpmp_gtfs/realtime/ tests/
git commit -m "feat: publish the upstream's own delay and drop the tracker"
```

---

### Task 11: Zapojení a dokumentace

**Files:**
- Modify: `src/dpmp_gtfs/cli.py`, `src/dpmp_gtfs/web/scheduler.py`, `docs/upstream-api.md`, `README.md`, `.env`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: vše z Tasků 2–10

- [ ] **Step 1: Zapoj CIS do rebuildu**

Ve `web/scheduler.py` před crawl vlož stažení archivů a stavbu rejstříku:

```python
    paths = await fetch_archives(settings.cis_urls, settings.cis_dir)
    index = build_index(paths, on_date=dt.date.today())
    timetable = await crawl(api, index)
```

a `api.buses()` na řádku 161 nahraď `api.vehicles()`. Snapshot času předej do `build_feed_message` jako `now`.

V `cli.py` udělej totéž pro `dump` i `serve`; `codes.json`/`stations.json` nahraď `stops.json`/`lines.json` a `connections`/`connectionDetail` jediným `connections/{line}/{n}` řízeným rejstříkem.

- [ ] **Step 2: Vyčisti .env**

```bash
grep -n "DPMP_API_KEY" .env && sed -i '' '/DPMP_API_KEY/d' .env
```

`DPMP_API_KEY` už nikdo nečte. Ostatní `DPMP_*` proměnné nech.

- [ ] **Step 3: Přepiš `docs/upstream-api.md`**

Nahraď celý soubor. Vezmi obsah ze specu `docs/superpowers/specs/2026-08-10-mhdonline-cis-migration-design.md` — sekce „Co se stalo", „Zdroj jízdních řádů: CIS", „Past na velikost písmen" a „Pole, která v `/connections` zmizela" jsou přesně to, co tenhle dokument má obsahovat. Doplň:

- že `state_dtime` už neexistuje (čas je na úrovni odpovědi `/vehicles`, v UTC s `Z`)
- že `gps_course` je stále `null` u všech vozidel
- že `/events` je stále vždy prázdné

V `README.md` oprav odstavce o `online.dpmp.cz` a o klíči v JS bundlu (řádky 5 a 45) — klíč nahradil rotující podpis, zdroj řádů je CIS.

- [ ] **Step 4: Spusť celou sadu a linters**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/ruff check src tests
```

Expected: vše prochází. Když `mypy` hlásí zbytky po `whole_number` nebo `Bus`, dočisti je.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: wire CIS into the rebuild and rewrite the upstream docs"
```

---

### Task 12: Křížová validace proti starému feedu

**Files:**
- Create: `scripts/cross_validate.py`
- Consumes: `tests/fixtures/reference/gtfs-old-api.zip` (Task 1), nově postavený `data/gtfs.zip`

- [ ] **Step 1: Postav feed z nových zdrojů**

```bash
.venv/bin/python -m dpmp_gtfs build 2>&1 | tail -20
ls -la data/gtfs.zip
```

Když CLI používá jiné jméno příkazu, zjisti ho z `cli.py`.

- [ ] **Step 2: Napiš porovnávací skript**

```python
# scripts/cross_validate.py
"""Compares a freshly built feed against the last one built from the old API.

Not a test. Some differences are expected -- the migration deliberately gives
up per-platform coordinates, and the timetable itself moved on between the two
builds -- so this reports rather than asserts. The point is that a human can
see at a glance whether anything changed that should not have.
"""

import csv
import io
import sys
import zipfile
from collections import defaultdict

KEYED = {
    "stops.txt": "stop_id",
    "routes.txt": "route_id",
    "trips.txt": "trip_id",
}


def read(path: str, name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as z:
        with z.open(name) as fh:
            return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf8")))


def stop_times_by_trip(path: str) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in read(path, "stop_times.txt"):
        out[row["trip_id"]].append((row["stop_id"], row["departure_time"]))
    return out


def compare_keyed(old: str, new: str, name: str, key: str) -> None:
    a = {r[key]: r for r in read(old, name)}
    b = {r[key]: r for r in read(new, name)}
    print(f"\n=== {name} ===")
    print(f"  old {len(a)}, new {len(b)}")
    only_old, only_new = sorted(a.keys() - b.keys()), sorted(b.keys() - a.keys())
    print(f"  jen ve starém: {len(only_old)} {only_old[:10]}")
    print(f"  jen v novém  : {len(only_new)} {only_new[:10]}")

    changed: dict[str, int] = defaultdict(int)
    for k in a.keys() & b.keys():
        for field in a[k]:
            if field in b[k] and a[k][field] != b[k][field]:
                changed[field] += 1
    for field, count in sorted(changed.items(), key=lambda kv: -kv[1]):
        note = ""
        if name == "stops.txt" and field in ("stop_lat", "stop_lon"):
            note = "  <- OČEKÁVANÉ: nástupiště dědí bod stanice"
        print(f"  změněno {field}: {count}{note}")


def compare_stop_times(old: str, new: str) -> None:
    a, b = stop_times_by_trip(old), stop_times_by_trip(new)
    shared = a.keys() & b.keys()
    same = sum(1 for t in shared if a[t] == b[t])
    print("\n=== stop_times.txt ===")
    print(f"  společných spojů: {len(shared)}")
    print(f"  identická sekvence i časy: {same}")
    differing = [t for t in sorted(shared) if a[t] != b[t]]
    print(f"  odlišných: {len(differing)} {differing[:10]}")
    for trip in differing[:3]:
        print(f"    {trip}\n      starý {a[trip][:4]}\n      nový  {b[trip][:4]}")


def main() -> None:
    old, new = sys.argv[1], sys.argv[2]
    for name, key in KEYED.items():
        compare_keyed(old, new, name, key)
    compare_stop_times(old, new)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Spusť porovnání**

```bash
.venv/bin/python scripts/cross_validate.py \
  tests/fixtures/reference/gtfs-old-api.zip data/gtfs.zip 2>&1 | tee /tmp/cross-validate.txt
```

- [ ] **Step 4: Vyhodnoť výstup**

Očekávané a v pořádku:

- `stops.txt`: změněné `stop_lat`/`stop_lon` u všech `S*P*` — nástupiště dědí bod stanice.
- `stops.txt`: přibylo/ubylo pár zastávek podle toho, kudy zrovna vedou objížďky.
- `trips.txt`, `stop_times.txt`: rozdíly u spojů, které DPMP mezi 8. 8. a dneškem reálně změnil.

**Vyžaduje vysvětlení, než se úkol uzavře:**

- Jakýkoli rozdíl v `route_id` nebo v množině `route_short_name` — linky se neměnily.
- Změněné `direction_id` u velké části spojů — znamenalo by, že `_out`/`_in` čteme obráceně.
- `stop_times` odlišné u víc než zhruba desetiny společných spojů.
- Chybějící `platform_code` nebo `parent_station`.

Zapiš závěr do `docs/superpowers/plans/2026-08-10-mhdonline-cis-migration.md` jako sekci „Výsledek křížové validace" — pár vět, co sedělo a co ne.

- [ ] **Step 5: Commit**

```bash
git add scripts/cross_validate.py docs/superpowers/plans/2026-08-10-mhdonline-cis-migration.md
git commit -m "test: cross-validate the migrated feed against the old-API reference"
```

---

---

### Task 13: Fáze buildu v logu i na mapě

Studený start stahuje ~305 MB archivů, projde 3,6 GB XML a crawluje ~2 700 spojů. Dnes je to několik minut ticha, a mapa přitom ukáže `"Trasy se nepodařilo načíst"` — nepravdu, protože nic neselhalo, data se teprve načítají.

Jedna fáze slouží dvěma spotřebitelům: operátorovi v logu a uživateli na mapě. Proto jeden zdroj, ne dvě nezávislé hlášky.

**Files:**
- Modify: `src/dpmp_gtfs/web/scheduler.py`, `src/dpmp_gtfs/web/app.py`, `src/dpmp_gtfs/web/static/map.js`, `src/dpmp_gtfs/cis/archive.py`, `src/dpmp_gtfs/cis/index.py`, `src/dpmp_gtfs/static/crawler.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `FeedState.static_phase: str | None` — `None` když se nestaví, jinak lidsky čitelný popis fáze. `_status()` ho vystavuje jako `status["static"]["phase"]`.

- [ ] **Step 1: Napiš padající testy**

```python
# tests/test_web.py
def test_status_reports_no_phase_when_idle(client):
    assert client.get("/healthz").json()["static"]["phase"] is None


def test_status_reports_the_phase_while_building(client, scheduler):
    scheduler.state.static_phase = "stahuji rejstřík CIS"
    assert client.get("/healthz").json()["static"]["phase"] == "stahuji rejstřík CIS"
```

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_web.py -v`
Expected: FAIL — `KeyError: 'phase'`

- [ ] **Step 3: Přidej fázi do stavu a do statusu**

Do `FeedState` přidej pole:

```python
    static_phase: str | None = None
    """What the static build is doing right now, or ``None`` when idle.

    Read by two consumers that must not drift apart: the log an operator
    watches and the message the map shows. A cold start is several minutes of
    downloading and crawling, and without this the service looks hung.
    """
```

V `_status()` do bloku `"static"` přidej `"phase": state.static_phase,`.

- [ ] **Step 4: Nastavuj a loguj fáze v jednom kroku**

V `scheduler.py` přidej pomocnou metodu a použij ji v `rebuild_static` kolem každé fáze:

```python
    def _phase(self, message: str) -> None:
        """Announce a build phase to both the log and the status endpoint."""
        self.state.static_phase = message
        logger.info("static build: %s", message)
```

Fáze: `"stahuji rejstřík CIS"` → `"čtu rejstřík"` → `"stahuji jízdní řády"` → `"počítám trasy"`. Na konci `rebuild_static`, i při chybě, nastav `self.state.static_phase = None` (`finally`).

- [ ] **Step 5: Doplň chybějící logy v dlouhých krocích**

`cis/archive.py` loguje až po stažení. Přidej řádek **před** requestem: `logger.info("fetching %s", url)`. `cis/index.py` loguje až po parsování — přidej řádek před ním s počtem archivů. `crawler.py` ohlásí start a konec, ale mezi nimi je ~6 minut ticha; loguj postup po linkách (`"line %s: %d/%d trips"`).

- [ ] **Step 6: Ukaž to na mapě**

V `map.js` je `paintStatus()` na sekundovém intervalu a `statusEl`. Přidej dotaz na `/healthz` a když `static.phase` není `null`, zobraz ji jako průběh (`"Načítám data: " + phase`). Uprav i `.catch` větve na `/coverage.geojson` a `/vehicles.json`: když je fáze nastavená, nesmí tvrdit, že se něco nepodařilo — data prostě ještě nejsou.

- [ ] **Step 7: Spusť testy a linters**

Run: `.venv/bin/python -m pytest && .venv/bin/python -m mypy && .venv/bin/ruff check src tests`
Expected: vše prochází.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: report build phases in the log and on the map"
```

---

### Task 14: Seznam spojů a směr z API

CIS se ukázal jako zdroj, který nemá jak dohnat. Změřeno proti živému API: 3 linky z 32 se rozešly (12 o 28,5 %, 9 o 15,1 %, 3 o 7,1 %), zbylých 29 sedí přesně, a rozchod nesouvisí se stářím verze — linka 1 má verzi stejně starou jako linka 12 a nulovou odchylku. Je to událost na lince, ne stárnutí. Dohromady nám to bere 63 z 2 762 spojů, a **práh to neřeší**: rozhoduje jen mezi hlasitým selháním a tichou dírou, protože spoj, o kterém CIS neví, nikdy nedohledáme.

Tenhle úkol přidá obojí, co CIS dodával, z API. CIS zůstává na místě až do Tasku 15.

**Files:**
- Create: `src/dpmp_gtfs/static/discovery.py`, `src/dpmp_gtfs/static/direction.py`, `tests/test_discovery.py`, `tests/test_direction.py`

**Interfaces:**
- Produces:
  - `discovery.discover_trips(api, line_id: str, stop_after: int = 50) -> list[int]` — čísla spojů linky, zjištěná procházením číselné řady
  - `direction.assign_directions(connections: dict[int, Connection]) -> dict[int, int]` — číslo spoje na `direction_id`

- [ ] **Step 1: Napiš padající testy pro dohledávání**

```python
# tests/test_discovery.py
from dpmp_gtfs.static.discovery import discover_trips


class FakeApi:
    def __init__(self, present: set[int]) -> None:
        self.present = present
        self.asked: list[int] = []

    async def connection(self, line: str, number: int):
        self.asked.append(number)
        return object() if number in self.present else None


async def test_finds_a_contiguous_run():
    api = FakeApi({1, 2, 3})
    assert await discover_trips(api, "1", stop_after=5) == [1, 2, 3]


async def test_crosses_gaps_smaller_than_the_stop_rule():
    # The largest gap measured anywhere in the real network is 18.
    api = FakeApi({1, 20, 21})
    assert await discover_trips(api, "1", stop_after=25) == [1, 20, 21]


async def test_stops_after_enough_consecutive_misses():
    api = FakeApi({1})
    assert await discover_trips(api, "1", stop_after=5) == [1]
    assert max(api.asked) == 6


async def test_an_empty_line_yields_nothing():
    api = FakeApi(set())
    assert await discover_trips(api, "99", stop_after=3) == []
```

- [ ] **Step 2: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_discovery.py -v`
Expected: FAIL — modul neexistuje.

- [ ] **Step 3: Implementuj dohledávání**

```python
# src/dpmp_gtfs/static/discovery.py
"""Finds which trips a line runs, by walking its trip-number space.

The API answers ``connections/{line}/{number}`` for one trip at a time and
publishes no listing. The numbers are sparse -- line 1 runs 206 trips spread
over ids up to 441 -- so the walk cannot stop at the first miss. It stops once
enough consecutive numbers come back empty.

The threshold is measured, not guessed: across the whole network the largest
gap between consecutive trip numbers is 18, so 50 leaves an ample margin while
keeping the tail short.
"""

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_STOP_AFTER = 50
BLOCK = 25
"""How many numbers to probe at once. Keeps the walk concurrent without
overshooting the stop rule by more than a block."""


class SupportsConnection(Protocol):
    async def connection(self, line: str, number: int) -> object | None: ...


async def discover_trips(
    api: SupportsConnection, line_id: str, stop_after: int = DEFAULT_STOP_AFTER
) -> list[int]:
    """Every trip number the API answers for, in ascending order."""
    found: list[int] = []
    misses = 0
    start = 1

    while misses < stop_after:
        block = range(start, start + BLOCK)
        results = await asyncio.gather(*(api.connection(line_id, n) for n in block))
        for number, result in zip(block, results, strict=True):
            if result is None:
                misses += 1
            else:
                found.append(number)
                misses = 0
        start += BLOCK

    logger.info("line %s: found %d trips up to %d", line_id, len(found), found[-1] if found else 0)
    return found
```

- [ ] **Step 4: Napiš padající testy pro směr**

Směr se neodvozuje z parity ani z kanonického pořadí — obojí bylo změřeno a selhalo (kanonické pořadí dalo na lince 1 nula shod z 206, protože referenční spoj byl náhodou opačný). Odvozuje se z **koncových zastávek**: spoje se seskupí podle první zastávky, a měřením na osmi linkách je ověřeno, že žádná taková skupina nikdy nepřesahuje přes oba směry.

```python
# tests/test_direction.py
from dpmp_gtfs.api.models import Connection
from dpmp_gtfs.static.direction import assign_directions


def _conn(number: int, stops: list[int]) -> Connection:
    return Connection.model_validate({
        "lineId": "1", "connectionId": number, "fixedCodes": ["X"],
        "stops": [{"stopId": s, "platformId": "1", "departureTime": "04:00:00"} for s in stops],
    })


def test_opposite_runs_get_opposite_ids():
    out = assign_directions({1: _conn(1, [10, 20, 30]), 2: _conn(2, [30, 20, 10])})
    assert out[1] != out[2]
    assert set(out.values()) == {0, 1}


def test_trips_sharing_a_terminal_share_a_direction():
    out = assign_directions({
        1: _conn(1, [10, 20, 30]),
        3: _conn(3, [10, 20]),        # a short turn, same way
        2: _conn(2, [30, 20, 10]),
    })
    assert out[1] == out[3] != out[2]


def test_the_label_is_stable_not_arbitrary():
    # Same input in a different order must produce the same labels, or a
    # rebuild would flip direction_id for no reason.
    a = assign_directions({1: _conn(1, [10, 30]), 2: _conn(2, [30, 10])})
    b = assign_directions({2: _conn(2, [30, 10]), 1: _conn(1, [10, 30])})
    assert a == b


def test_a_single_direction_line_is_all_zero():
    out = assign_directions({1: _conn(1, [10, 20]), 3: _conn(3, [10, 20])})
    assert set(out.values()) == {0}
```

- [ ] **Step 5: Spusť a ověř pád**

Run: `.venv/bin/python -m pytest tests/test_direction.py -v`
Expected: FAIL — modul neexistuje.

- [ ] **Step 6: Implementuj směr**

```python
# src/dpmp_gtfs/static/direction.py
"""Splits a line's trips into its two directions.

The API states no direction anywhere: a trip is a line id, a trip number, its
fixed codes and its stops. Their own app does not need one either -- it shows a
destination, not a direction. ``direction_id`` is a GTFS construct, and GTFS
only asks that the two directions be told apart consistently; which one is 0
carries no meaning.

So it is derived from where trips start and end. Trips leaving the same
terminal run the same way, which was checked against the CIS registry on eight
lines covering 1,100 trips: every terminal group fell wholly inside one
direction, never across both. Lines have more than two groups -- short turns
and variant terminals -- so the groups are then paired up: a group that starts
where another ends is its opposite.

Two approaches were measured and rejected. Trip-number parity matches today on
every trip in the network, but it is a JDF numbering convention nothing
guarantees. Ordering stops against the line's longest trip cannot say which of
the two runs is 0: on line 1 it labelled all 206 trips backwards, because the
reference trip happened to run inbound.
"""

import logging
from collections import defaultdict

from dpmp_gtfs.api.models import Connection

logger = logging.getLogger(__name__)


def assign_directions(connections: dict[int, Connection]) -> dict[int, int]:
    """``{trip number: 0 or 1}`` for one line's trips."""
    ends: dict[int, tuple[int, int]] = {}
    for number, connection in connections.items():
        if connection.stops:
            ends[number] = (connection.stops[0].stop_id, connection.stops[-1].stop_id)

    groups: dict[int, set[int]] = defaultdict(set)
    for number, (first, _) in ends.items():
        groups[first].add(number)

    # A group is the opposite of the one that starts where this one ends.
    starts = set(groups)
    opposite: dict[int, int] = {}
    for first, numbers in groups.items():
        last = ends[next(iter(numbers))][1]
        if last in starts and last != first:
            opposite[first] = last

    # Two-colour the groups. Sorting first keeps the labels stable across
    # rebuilds: an unstable rule would flip direction_id for no reason.
    colour: dict[int, int] = {}
    for first in sorted(groups):
        if first in colour:
            continue
        colour[first] = 0
        if (other := opposite.get(first)) is not None and other not in colour:
            colour[other] = 1

    out = {n: colour.get(first, 0) for n, (first, _) in ends.items()}
    unpaired = sorted(set(groups) - set(opposite))
    if unpaired:
        logger.debug("terminals %s have no opposite run; treated as direction 0", unpaired)
    return out
```

- [ ] **Step 7: Spusť testy a linters**

Run: `.venv/bin/python -m pytest tests/test_discovery.py tests/test_direction.py -v && .venv/bin/ruff check src tests && .venv/bin/python -m mypy`
Expected: vše prochází.

- [ ] **Step 8: Commit**

```bash
git add src/dpmp_gtfs/static/discovery.py src/dpmp_gtfs/static/direction.py tests/test_discovery.py tests/test_direction.py
git commit -m "feat: discover trips and derive direction from the API alone"
```

---

### Task 15: Odstranit CIS

Teprve teď, když API dodá seznam i směr, se rejstřík odpojí a smaže.

**Files:**
- Delete: `src/dpmp_gtfs/cis/`, `tests/test_cis_index.py`, `tests/test_cis_archive.py`, `tests/fixtures/netex/`
- Modify: `src/dpmp_gtfs/static/crawler.py`, `src/dpmp_gtfs/types.py`, `src/dpmp_gtfs/config.py`, `src/dpmp_gtfs/web/scheduler.py`, `src/dpmp_gtfs/cli.py`, `pyproject.toml`, `docs/upstream-api.md`, `README.md`
- Test: `tests/test_crawler.py`

- [ ] **Step 1: Přepiš crawler na dohledávání**

`crawl(api, attempts, backoff)` už nebere `ServiceIndex`. Pro každou linku z `api.lines()` zavolá `discover_trips`, stáhne nalezené spoje a směr doplní `assign_directions` nad staženými spoji té linky. `MISSING_TRIP_LIMIT` i celá logika chybějících spojů mizí — bez druhého zdroje není s čím se rozcházet. Uprav `tests/test_crawler.py`: testy o driftu a prahu smaž (jejich předmět neexistuje), testy o retry chování a o tvaru `Timetable` zachovej.

- [ ] **Step 2: Odpoj rejstřík z nastavení a vstupních bodů**

Z `config.py` smaž `cis_urls` a `cis_dir`. Ze `scheduler.py` a `cli.py` odstraň `fetch_archives`/`build_index` z rebuildu. Z `pyproject.toml` smaž `defusedxml` a `types-defusedxml`.

- [ ] **Step 3: Smaž balík a jeho testy**

```bash
git rm -r src/dpmp_gtfs/cis tests/test_cis_index.py tests/test_cis_archive.py tests/fixtures/netex
rm -rf data/cis
```

- [ ] **Step 4: Přepiš dokumentaci**

V `docs/upstream-api.md` nahraď celou sekci o CIS popisem toho, proč se opustil — s naměřenými čísly (3 linky z 32 rozešlé, 63 z 2 762 spojů, rozchod nesouvisí se stářím verze) a s tím, že práh volí jen mezi hlasitým selháním a tichou dírou. Dopiš, jak se zjišťuje seznam spojů a odkud pochází `direction_id`. Sraz na to i `README.md`.

- [ ] **Step 5: Spusť všechno**

Run: `.venv/bin/python -m pytest && .venv/bin/python -m mypy && .venv/bin/ruff check src tests`
Expected: vše prochází, žádná zmínka o `cis` nikde v `src/` ani `tests/`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: drop the CIS registry, the API is now the only source"
```

---

## Výsledek křížové validace

_Vyplní Task 12._
