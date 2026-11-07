# dpmp-to-gtfsr

GTFS a GTFS-Realtime feed pro pardubickou MHD.

Dopravní podnik města Pardubic provozuje [online.dpmp.cz](https://online.dpmp.cz/), která ukazuje
polohu spojů v reálném čase, ale nepublikuje žádný export v průmyslovém standardu. Její backend
je `api.mhdonline.cz`: odtud jsou zastávky a polohy vozů. Jízdní řád z něj číst nejde, ten je
z veřejného registru [CIS JŘ](https://portal.cisjr.cz/), kam ho DPMP odevzdává (podrobnosti
v [`docs/upstream-api.md`](docs/upstream-api.md)).

Chybí ale dvě věci: **GTFS jako standardní formát**, na kterém staví většina zahraničních
konzumentů (Google Maps pardubickou MHD nezná), a hlavně **realtime** — polohy vozidel a
zpoždění nejsou nikde v tomhle formátu k dispozici, přestože je DPMP ve své aplikaci ukazuje.

Tahle služba ten most staví: z registru a veřejného API aplikace sestaví
[GTFS](https://gtfs.org/schedule/) i [GTFS Realtime](https://gtfs.org/realtime/) a vystaví je
přes HTTP.

> **Neoficiální projekt.** Není provozovaný ani schválený DPMP. Staví na CIS JŘ a na veřejně
> dostupném API jejich vlastní webové aplikace. Ideálním výsledkem je, že feed převezme sám dopravní podnik.

## Feedy

| cesta | obsah |
|---|---|
| `GET /gtfs.zip` | statický feed (jízdní řády, zastávky, linky) |
| `GET /gtfs-rt.pb` | realtime — polohy vozidel a predikce příjezdů |
| `GET /gtfs-rt.json` | totéž v JSON, pro ladění |
| `GET /healthz` | stáří feedů a poslední chyba; `healthy` je `false` i když selhala přestavba, stavový kód 503 jen když není co vydat (`serving`) |

Homepage se stavem služby je na `/`, dokumentace feedů na `/docs`.

> **Trasy v `shapes.txt` jsou odhad.** Zastávky, časy a spoje pocházejí přímo
> z dat dopravního podniku, ale geometrii jízdy po ulicích v použitelné podobě
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
uv run dpmp-gtfs build-static    # jednou (registr CIS + zastávky + geometrie)
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

První start postaví feed od nuly (300 MB archivů CIS, dva dotazy na API a geometrie
tras), další starty ho načtou z volume (~5 s) a postaví ho hned jen tehdy, když je
starší než 30 hodin. Archivy se podruhé jen ověřují podmíněným dotazem.

Nasazení na veřejnou adresu:

- **Kontejnerový obraz** `ghcr.io/xaralis/dpmp-to-gtfsr` publikuje workflow
  [Image](.github/workflows/image.yml) při každém pushi do `main` (tag `sha-<commit>` a `latest`).
- **[Vlastní instance](docker/deploy-lightsail.md)** — když má služba stroj pro sebe.

Všechny volby jdou nastavit proměnnými s prefixem `DPMP_` — interval obnovy,
hodina noční přestavby, vypnutí geometrie tras a další, viz
[`config.py`](src/dpmp_gtfs/config.py).

Chyby umí služba hlásit do Sentry nebo čehokoliv, co mluví jeho protokolem
(v produkci self-hostovaný GlitchTip). Zapíná se proměnnou `SENTRY_DSN`; bez
ní se nikam nic neposílá. Hlásí se jen chyby, žádné traces ani osobní údaje —
včetně těch, které služba sama zachytí a jen zaloguje, třeba nepovedené noční
přestavby. `SENTRY_ENVIRONMENT` je výchozí `production`, `SENTRY_RELEASE`
nastavuje obraz na commit, ze kterého vznikl.

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
- **Čísla spojů API a CIS nejsou totéž.** Asi třetina vozů hlásí číslo, které je v CIS jiná jízda
  téže linky, proto se vůz páruje podle příští zastávky a času, ne podle čísla.
- **Souřadnice zastávky jsou nepovinné** — jedna zastávka (`Opočínek,rozvodna`) je publikovaná
  úplně bez nich, a feed takové zastávky přeskočí.
- **Nástupiště ve feedu nejsou.** `/stops` vrací jeden bod na stanici a CIS nástupiště nezná, takže
  zastávky jsou `S16`, ne `S16P2`.
- **`gps_course` je vždy `null`** — ve všech nahraných snímcích, u všech vozidel, stejně jako
  u staršího API. Směr vozidla se proto počítá z jízdního řádu, ne z kompasu vozu.

Podrobnosti i s důkazy v [`docs/upstream-api.md`](docs/upstream-api.md).

### Jízdní řád je z CIS, ne z API

Spoje, časy a dny provozu se berou z **celostátního registru CIS JŘ**
(`portal.cisjr.cz`), kam DPMP jízdní řády odevzdává jako primární zdroj.
`api.mhdonline.cz` odpovídá na `connections/{linka}/{spoj}` jen pro spoj, na kterém
zrovna jede vůz, u ostatních vrací 403, ať se ptá kdokoli. Vyvěšené jízdní řády
na dpmp.cz sedí na feed na 195 ze 205 porovnaných kombinací zastávky a dne včetně
prázdninového režimu.

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
  souřadnice zastávek — ty se proto berou z `api.mhdonline.cz`.

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
