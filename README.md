# dpmp-to-gtfsr

GTFS a GTFS-Realtime feed pro pardubickou MHD.

Dopravní podnik města Pardubic provozuje [online.dpmp.cz](https://online.dpmp.cz/), která ukazuje
polohu spojů v reálném čase, ale nepublikuje žádný export v průmyslovém standardu. Její backend
je `api.mhdonline.cz` — hromadný seznam spojů ale ani ten neumí, takže si ho tenhle projekt bere
z celostátního CIS (`portal.cisjr.cz`), kam DPMP jízdní řády odevzdává jako primární zdroj. Po
téže cestě se pardubická MHD dostává i do Mapy.cz.

Chybí ale dvě věci: **GTFS jako standardní formát**, na kterém staví většina zahraničních
konzumentů (Google Maps pardubickou MHD nezná), a hlavně **realtime** — polohy vozidel a
zpoždění nejsou nikde v tomhle formátu k dispozici, přestože je DPMP ve své aplikaci ukazuje.

Tahle služba ten most staví: z veřejného API aplikace a z CIS sestaví
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

> **Trasy v `shapes.txt` jsou odhad.** Zastávky, časy a spoje pocházejí přímo
> z API dopravního podniku, ale geometrii jízdy po ulicích v použitelné podobě
> nikdo nepublikuje, takže se dopočítává routováním. Podrobnosti níž.

## Spuštění

Konfigurace je v `.env` **v kořeni repozitáře** — tam ho hledá jak služba
samotná, tak všechny příkazy níž. Žádná proměnná není povinná: výchozí hodnoty
v [`config.py`](src/dpmp_gtfs/config.py) fungují bez úprav.

Autentizace vůči `api.mhdonline.cz` je rotující podpis v hlavičce
`X-App-Protocol` (`HMAC-SHA256` ze semínka a 15minutového okna), ne klíč
v těle požadavku jako u starého API. Semínko je `your-public-protocol-seed` —
placeholder ze šablony, který DPMP nechalo v produkčním JS bundlu aplikace.
Není to tajemství, ale do repozitáře nepatří o nic víc než starý klíč: kdyby
se to jednou změnilo, jde přepsat proměnnou `DPMP_PROTOCOL_SEED`, aby výměna
byla restart, ne commit.

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

- **Čas snímku je jeden na celou odpověď `/vehicles`** (pole `time`, v UTC se sufixem `Z`),
  ne u každého vozidla zvlášť.
- **`currentDelay` chybějící nebo `null` znamená, že se pro to vozidlo nepublikuje žádný
  `TripUpdate`** — chybějící údaj není nula. Přítomné, ale nerozparsovatelné `currentDelay` se
  naopak zaloguje a počítá jako nulové zpoždění, takže spoj v feedu zůstane.
- **Souřadnice zastávky jsou nepovinné** — jedna zastávka (`Opočínek,rozvodna`) je publikovaná
  úplně bez nich, a feed takové zastávky přeskočí.
- **`gps_course` je vždy `null`** — ve všech nahraných snímcích, u všech vozidel, stejně jako
  u staršího API. Směr vozidla se proto počítá z jízdního řádu, ne z kompasu vozu.

Podrobnosti i s důkazy v [`docs/upstream-api.md`](docs/upstream-api.md).

### Trasy jsou odhad

`shapes.txt` nevzniká z podkladů dopravního podniku, ale **zroutováním zastávek přes
[Valhallu](https://valhalla.github.io/valhalla/) nad OpenStreetMap**. Je to tedy
nejpravděpodobnější cesta po ulicích mezi zastávkami, ne doložené vedení linky.
Výsledky se cachují v `shape-cache.json`, takže běžná noční přestavba nepošle na router
ani jeden dotaz.

Pokusili jsme se získat data od zdroje a nestačí na to:

- Staré `online.dpmp.cz/api` mělo endpoint `/api/route?line=N` s geometrií, ale vracelo
  **jednu reprezentativní trasu na linku**, ne na spoj. Linky mají varianty — zkrácené
  obraty, jiné větve — které se od ní liší i o kilometry, takže z ní `shapes.txt` sestavit
  nejde. Per-spoj geometrii nemá ani vlastní aplikace dopravního podniku. Nástupce
  `api.mhdonline.cz` obdobný endpoint nenabízí vůbec.
- Změřeno na 218 sekvencích zastávek: zroutované trasy míjejí zastávky, které mají
  obsloužit, o 8–11 m, geometrie ze starého API o stovky metrů. Ani na 98 sekvencích, kde
  sedí nejlíp, nevyhraje ani jednou.
- NeTEx z registru CIS je mechanický převod z JDF a geometrii tras nenese; nemá dokonce ani
  souřadnice zastávek — ty se proto dál berou z `api.mhdonline.cz`.

**Co z toho plyne pro konzumenty:** zastávky, časy a spoje berte jako data dopravního
podniku, tvar trasy mezi zastávkami jako odhad. Routování může zvolit jinou, byť
průjezdnou ulici — pár desítek takových míst je známo a nikdo je neprošel proti
skutečnému vedení linek.

## Předchůdce

Starší pokus [xaralis/dpmp-gtfs](https://github.com/xaralis/dpmp-gtfs) stavěl statický feed z JDF
dat CIS přes [jrutil](https://gitlab.com/dvdkon/jrutil) a vyžadoval .NET i ručně dodávané
proprietární soubory. Tenhle projekt ho nahrazuje — bez .NET, bez ručních zásahů, a s realtime
navíc.

## Poděkování

Původní verze stála na [jrutil](https://gitlab.com/dvdkon/jrutil) Davida Koňaříka. Bez jeho práce
na převodu JDF → GTFS by první krok byl mnohem těžší.

## Licence

AGPL-3.0-or-later, viz [LICENSE](LICENSE).
