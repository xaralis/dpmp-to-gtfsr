# dpmp-to-gtfsr

GTFS a GTFS-Realtime feed pro pardubickou MHD.

Dopravní podnik města Pardubic provozuje [online.dpmp.cz](https://online.dpmp.cz/), která ukazuje
polohu spojů v reálném čase, ale nepublikuje žádný export v průmyslovém standardu.

Jízdní řády se do některých vyhledávačů dostávají oklikou přes celostátní CIS — Mapy.cz
pardubickou MHD mají. Chybí ale dvě věci: **GTFS jako standardní formát**, na kterém staví
většina zahraničních konzumentů (Google Maps pardubickou MHD nezná), a hlavně **realtime** —
polohy vozidel a zpoždění nejsou nikde k dispozici, přestože je DPMP ve své aplikaci ukazuje.

Tahle služba ten most staví: z veřejného API aplikace sestaví
[GTFS](https://gtfs.org/schedule/) i [GTFS Realtime](https://gtfs.org/realtime/) a vystaví je
přes HTTP.

> **Neoficiální projekt.** Není provozovaný ani schválený DPMP. Staví na veřejně dostupném API
> jejich vlastní webové aplikace. Ideálním výsledkem je, že feed převezme sám dopravní podnik.

## Feedy

| cesta | obsah |
|---|---|
| `GET /gtfs.zip` | statický feed (jízdní řády, zastávky, linky) |
| `GET /gtfs-rt.pb` | realtime — polohy vozidel a predikce příjezdů |
| `GET /gtfs-rt.json` | totéž v JSON, pro ladění |
| `GET /healthz` | stáří feedů a poslední chyba |

Homepage se stavem služby je na `/`, dokumentace feedů na `/docs`.

## Spuštění

Konfigurace je v `.env` **v kořeni repozitáře** — tam ho hledá jak služba
samotná, tak všechny příkazy níž. Povinná je jediná proměnná:

```bash
echo 'DPMP_API_KEY=...' > .env
```

Klíč je ten, který veřejná aplikace DPMP posílá ze svého JS bundlu
(`online.dpmp.cz`, chunk `pages/lines-*.js`). Není to tajemství, ale do
repozitáře nepatří — jeho výměna má být restart, ne commit.

### Bez Dockeru

```bash
uv sync
uv run dpmp-gtfs build-static    # jednou, ~7 min (jízdní řády + geometrie)
uv run dpmp-gtfs serve           # http://localhost:8000
```

`build-static` je volitelný: `serve` si feed postaví sám, když v `data/`
žádný nenajde. Předem je to ale příjemnější — jinak služba prvních sedm minut
neodpovídá, protože uvicorn otevře port až po dokončení startu.

Další příkazy:

```bash
uv run dpmp-gtfs --help
uv run dpmp-gtfs serve --port 9000 --reload
```

### S Dockerem

```bash
docker compose --env-file .env -f docker/compose.yaml up -d
```

`--env-file .env` není volitelné: bez něj by compose hledal `docker/.env`,
tedy jiný soubor než ten, ze kterého čte `dpmp-gtfs serve`.

První start postaví feed od nuly (~7 min včetně geometrie tras), další starty
ho načtou z volume (~5 s).

Nasazení na veřejnou adresu:

- **[Cloudflare Tunnel](docker/deploy-tunnel.md)** — pro stroj, kde už něco běží.
  Žádné porty, žádný nginx, žádný certbot, žádný zásah do toho, co tam je.
- **[Vlastní instance](docker/deploy-lightsail.md)** — když má služba stroj pro sebe.

Všechny volby jdou nastavit proměnnými s prefixem `DPMP_` — interval obnovy,
hodina noční přestavby, vypnutí geometrie tras a další, viz
[`config.py`](src/dpmp_gtfs/config.py).

## Vývoj

```bash
uv sync
uv run pytest
uv run ruff check .
```

## Poznámky k datům

Několik vlastností zdrojového API, které nejsou zřejmé a stály za ověření:

- **`state_dtime` je v UTC**, zatímco všechny jízdní řády jsou v lokálním čase.
- **`time_difference` není zpoždění**, ale odpočet do plánovaného odjezdu z aktuální zastávky.
  Skutečné zpoždění se měří sledováním přechodů mezi zastávkami — viz
  [`realtime/tracker.py`](src/dpmp_gtfs/realtime/tracker.py).
- **`current_stop_number`** kóduje stanici i nástupiště jako `stanice * 100 + nástupiště`,
  kdežto `last_stop_number` nese jen holé číslo stanice.
- **Geometrie tras v API není.** `shapes.txt` vzniká zroutováním zastávek přes
  [Valhallu](https://valhalla.github.io/valhalla/) nad OpenStreetMap. Výsledky se cachují
  v `shape-cache.json`, takže běžná noční přestavba nepošle na router ani jeden dotaz.

## Předchůdce

Starší pokus [xaralis/dpmp-gtfs](https://github.com/xaralis/dpmp-gtfs) stavěl statický feed z JDF
dat CIS přes [jrutil](https://gitlab.com/dvdkon/jrutil) a vyžadoval .NET i ručně dodávané
proprietární soubory. Tenhle projekt ho nahrazuje — všechna data pocházejí z jednoho API.

## Poděkování

Původní verze stála na [jrutil](https://gitlab.com/dvdkon/jrutil) Davida Koňaříka. Bez jeho práce
na převodu JDF → GTFS by první krok byl mnohem těžší.

## Licence

AGPL-3.0-or-later, viz [LICENSE](LICENSE).
