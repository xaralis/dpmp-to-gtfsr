# dpmp-to-gtfsr

GTFS a GTFS-Realtime feed pro pardubickou MHD.

Dopravní podnik města Pardubic provozuje [online.dpmp.cz](https://online.dpmp.cz/), která ukazuje
polohu spojů v reálném čase, ale nepublikuje žádný export v průmyslovém standardu. Její backend
je `api.mhdonline.cz` — hromadný seznam spojů ale ani ten neumí, takže si ho tenhle projekt
dohledává sám, spoj po spoji (podrobnosti v [`docs/upstream-api.md`](docs/upstream-api.md)).

Chybí ale dvě věci: **GTFS jako standardní formát**, na kterém staví většina zahraničních
konzumentů (Google Maps pardubickou MHD nezná), a hlavně **realtime** — polohy vozidel a
zpoždění nejsou nikde v tomhle formátu k dispozici, přestože je DPMP ve své aplikaci ukazuje.

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
uv run dpmp-gtfs build-static    # jednou, ~20 min (registr CIS + jízdní řády + geometrie)
uv run dpmp-gtfs serve           # http://localhost:8000
```

`build-static` je volitelný: `serve` si feed postaví sám, když v `data/`
žádný nenajde. Ta stavba běží na pozadí, takže služba naskočí okamžitě a než
je feed hotový, hlásí na `/healthz` i na mapě, co zrovna dělá. `/gtfs.zip`
do té doby vrací 503, protože ještě není co vydat.

První běh stáhne do `data/cis/` dva NeTEx archivy z CIS (~300 MB dohromady);
další běhy se ptají podmíněně a stahují je jen tehdy, když se registr změnil.

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

První start postaví feed od nuly (~20 min: 300 MB archivů CIS, ~4 400 dotazů
na API a geometrie tras), další starty ho načtou z volume (~5 s). Archivy se
podruhé jen ověřují podmíněným dotazem, takže noční přestavba je rychlejší.

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

Úplná přestavba statického feedu je zhruba 4 400 requestů a asi dvacet minut —
většinu z toho spolkne hledání konce každé linky, které potřebuje padesát 404
za sebou, aby si bylo jisté. Pro cokoliv, co se projeví až na konci buildu, se
proto hodí zapnout cache odpovědí:

```bash
DPMP_HTTP_CACHE=1 uv run dpmp-gtfs build-static
```

Ukládá i ty 404. Jízdní řády platí 12 hodin, `vehicles` a `events` pět minut,
neznámý endpoint taky pět minut. **V produkci zůstává vypnutá** — noční
přestavba musí vidět jízdní řád, který platí teď.

## Poznámky k datům

Několik vlastností zdrojového API, které nejsou zřejmé a stály za ověření:

- **Čas snímku je jeden na celou odpověď `/vehicles`** (pole `time`, v UTC se sufixem `Z`),
  ne u každého vozidla zvlášť.
- **`currentDelay` chybějící nebo `null` znamená, že se pro to vozidlo nepublikuje žádný
  `TripUpdate`** — chybějící údaj není nula. Přítomné, ale nerozparsovatelné `currentDelay` se
  naopak zaloguje a počítá jako nulové zpoždění, takže spoj v feedu zůstane.
- **Dny provozu z API neodpovídají jízdnímu řádu.** Zhruba u třetiny spojů říká `fixedCodes`
  jiný den, než má dopravní podnik vyvěšený: spoj 46 linky 1 (06:36 ze Slovany,točna) je podle
  API víkendový a podle jízdního řádu i podle CIS jede v pracovních dnech. Proto se dny provozu
  berou z CIS a z API jen zbytek. Doloženo v [`docs/dpmp-hlaseni-kalendare.md`](docs/dpmp-hlaseni-kalendare.md),
  nahlášeno dopravnímu podniku.
- **Souřadnice zastávky jsou nepovinné** — jedna zastávka (`Opočínek,rozvodna`) je publikovaná
  úplně bez nich, a feed takové zastávky přeskočí.
- **Souřadnice nástupišť zmizely.** `/stops` vrací jeden bod na stanici, takže všechna její
  nástupiště ho sdílejí. Proti datům starého API se tím posunulo 357 zastávkových bodů.
- **`gps_course` je vždy `null`** — ve všech nahraných snímcích, u všech vozidel, stejně jako
  u staršího API. Směr vozidla se proto počítá z jízdního řádu, ne z kompasu vozu.

Podrobnosti i s důkazy v [`docs/upstream-api.md`](docs/upstream-api.md).

### Dny provozu jsou z CIS, ne z API

Všechno ostatní ve feedu je z `api.mhdonline.cz`, ale **dny provozu se berou
z celostátního registru CIS JŘ** (`portal.cisjr.cz`), kam DPMP jízdní řády
odevzdává jako primární zdroj. Důvod je prostý: kódy dnů provozu z API zhruba
u třetiny spojů neodpovídají jízdnímu řádu, který DPMP samo vyvěšuje. Spoj 46
linky 1, 06:36 ze Slovany,točna — vyvěšený jízdní řád i CIS říkají pracovní
den, API říká víkend. Dopravnímu podniku je to nahlášené; než se to spraví,
platí CIS.

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
