# Migrace na api.mhdonline.cz a CIS

DPMP vyměnil backend. `online.dpmp.cz/api` je mrtvý — vrací 404 z nginx — a
projekt nemá funkční zdroj dat. Tenhle dokument popisuje, čím ho nahradit.

## Co se stalo

Nový backend je `api.mhdonline.cz` s prefixem podle provozovatele
(`/pardubice`). Proti starému stavu se změnilo všechno podstatné:

- **Autentizace.** Statický klíč v těle zmizel. Místo něj rotující podpis
  v hlavičce `X-App-Protocol`: `HMAC-SHA256(seed, floor(unix_ms / 900000))`
  v hexu, platný 15 minut. Seed je `your-public-protocol-seed` — placeholder ze
  šablony, který nechali v produkčním bundlu. Veřejný stejně jako předtím klíč.
- **Volání.** Všechno `GET`, žádné `Content-Type: text/plain` kouzlo.
- **Zmizel hromadný výpis spojů linky.** Starý `connections?line=X` byl základ
  `trips.txt`, `stop_times.txt` i `calendar.txt`. Náhrada v API neexistuje —
  ověřeno proti jejich vlastnímu JS bundlu, jejich frontend celý jízdní řád
  nikdy nezobrazuje.
- **Zmizel endpoint `codes`.** Význam kalendářních kódů se musí zadrátovat.
- **Zmizely souřadnice nástupišť.** `/stops` vrací 219 plochých zastávek
  s jedním bodem; `/pardubice/platforms` je 404.

Naopak přibylo:

- **`currentDelay` je skutečné zpoždění** v ISO-8601 (`"-PT1M43S"`), ne odpočet
  do plánovaného odjezdu jako staré `time_difference`.
- **`connectionId` u vozidla** rozřeší spoj napřímo.
- **`onStation`** říká, jestli vůz stojí v zastávce.

Kompletní seznam endpointů, vytažený z bundlu:

```
/{p}  /{p}/configuration/connectionDetail  /{p}/presets  /{p}/lines  /{p}/stops
/{p}/vehicles  /{p}/vehicles/movement  /{p}/events  /{p}/icons/{id}
/{p}/connections/{line}/{no}  /{p}/connections/{line}/longest
/{p}/paths/{a}/{b}  POST /{p}/paths  /{p}/stops/{id}/incomingArrivals
```

Dvě poznámky k tomu, co se dá snadno přečíst špatně: `/paths/{a}/{b}` je
dvojice **zastávek**, ne linka a spoj, a `route` je u většiny dvojic prázdné
pole. Tělo `POST /paths` se nepodařilo rozluštit (`GetPathsRequest`); nepotřebujeme
ho, geometrii dál routujeme přes Valhallu.

## Zdroj jízdních řádů: CIS

Chybějící seznam spojů dodá CIS JŘ (`portal.cisjr.cz`), kam DPMP jízdní řády
odevzdává jako primární zdroj.

**JDF nestačí.** `/pub/JDF/JDF.zip` pokrývá jen 19 z 32 linek — chybí přesně ty
čistě městské, protože trolejbusy jsou vedené jako „dráha městská", ne jako
linková doprava. Obě části jsou ale v NeTEx:

| archiv | linky | obsah |
|---|---|---|
| `/pub/netex/NeTEx_DrahyMestske.zip` (89 MB) | 13 | trolejbusy a čistě městské |
| `/pub/netex/NeTEx_VerejnaLinkovaDoprava.zip` (218 MB) | 20 | autobusy včetně příměstských |

Dohromady 33 linek 655xxx v jednom formátu, což pokrývá všech 32 linek, které
zná API. Rozdíl je linka 655101, kterou CIS eviduje a API ne; rejstřík ji
prostě nemá kam napojit a tiše vypadne. DPMP se v archivech pozná podle IČO
`63217066`.

### Klíčové zjištění: `connectionId` je číslo spoje z JDF

Statika z CIS a realtime z API jdou spojit napřímo. Ověřeno na lince 25:

```
CIS/JDF spoje    : [1, 3, 5, 7, 9, 11, 13, 15, 20, 22, 24]
API connectionIds: [1, 3, 5, 7, 9, 11, 13, 15, 20, 22, 24]
rozdíl v obou směrech prázdný
```

Proto zůstává `trip_id` ve tvaru `L9C115` beze změny a feed není breaking
change pro nikoho, kdo ho konzumuje.

### Výběr verze je nutný, ne volitelný

Linka je v NeTEx často vícekrát a **víc verzí platí současně**:

```
LINE-607  283 spojů  platnost 2026-01-01 -> 2030-12-31
LINE-396  206 spojů  platnost 2026-07-01 -> 2030-12-31
API linka 1: 206 spojů -> shoda s LINE-396, přesně
```

Sjednocení verzí by přidalo 141 neexistujících spojů. Pravidlo: **z verzí,
jejichž platnost pokrývá datum buildu, vyhrává ta s nejpozdějším `FromDate`.**
Při shodě rozhodne delší `ToDate`, pak jméno souboru — aby byl výběr
deterministický.

### Co CIS nedá

Souřadnice ani čísla nástupišť. `<Location />` je v každém `ScheduledStopPoint`
prázdný, `Quay` se v souborech nevyskytuje a odpovídající sloupce JDF jsou
prázdné. NeTEx z CIS je mechanický převod z JDF a to geografii neobsahuje.

## Architektura

Dva zdroje s ostře oddělenou rolí:

| zdroj | odpovídá za | neodpovídá za |
|---|---|---|
| CIS NeTEx | *které spoje existují* — rejstřík `(linka, spoj)` k datu | obsah spojů, nástupiště, souřadnice |
| api.mhdonline.cz | obsah — zastávky, časy, `platformId`, souřadnice, realtime | seznam spojů |

Zvažovaná alternativa byla postavit celý řád na NeTEx a API nechat jen na
souřadnice a realtime. Zamítnuta: CIS nemá nástupiště, takže by `platform_code`
z feedu zmizel, a znamenalo by to napsat plnohodnotný NeTEx parser místo
čtečky jmen.

### Nový balík `dpmp_gtfs/cis/`

**`archive.py`** stáhne oba zipy přes `httpx.AsyncClient.stream()` do
`data/cis/`, s `If-Modified-Since` podle uloženého `Last-Modified`. Na 304 se
nestahuje nic.

**`index.py`** projde položky archivu `zipfile` streamem, zahodí vše bez IČO
`63217066`, ze zbytku vytáhne `PublicCode` linky, jména `ServiceJourney`
a `ValidBetween`, aplikuje pravidlo výběru verze a vrátí `ServiceIndex`.

`ServiceIndex` slibuje ven jedinou věc: mapování `jdfId -> set[int]`, tedy
„pro tuhle linku existují k tomuhle datu tahle čísla spojů", plus metadata
o zvolené verzi. Sémantika NeTEx dál neprosakuje — builder o NeTEx neví.

Oba světy se potkávají jen v `lines[].jdfId`, které dá dvojici
`655001 <-> lineId "1"`.

### Změny v existujících modulech

| modul | změna |
|---|---|
| `config.py` | `api_root` -> `api.mhdonline.cz`, přibude `provider`, `protocol_seed`, cesty k CIS a `crawl_rate_limit`; `api_key` a `crawl_delay` mizí |
| `api/client.py` | GET místo POST, `X-App-Protocol` počítaný při každém volání, token bucket na 8 req/s |
| `api/models.py` | `Bus`->`Vehicle`, `Station`+`Platform`->`Stop`, `ConnectionDetail`->`Connection`; `currentDelay` na `timedelta` |
| `static/crawler.py` | bere `ServiceIndex`, tahá přesně známé `(linka, spoj)` |
| `static/calendar.py` | kódy z čísel na písmena JDF |
| `types.py` | `Timetable`: `stations`->`stops`, `codes` a `summaries` pryč |
| `upstream.py` | přepsat, dokumentuje neplatné vlastnosti |
| `realtime/feed.py` | zpoždění z vozidla místo z trackeru, `onStation` -> `current_status` |
| `realtime/tracker.py` | **smazat celý** |
| `docs/upstream-api.md` | přepsat |

`ids.py` zůstává beze změny. `builder.py`, `writer.py`, `shapes.py`
a `service_watch.py` se dotknou jen okrajově.

### Kalendář

Písmena pevných kódů podle konvence JDF, zadrátovaná místo mrtvého endpointu
`codes`:

| kód | význam |
|---|---|
| `X` | jede v pracovních dnech |
| `6` | jede v sobotu |
| `+` | jede v neděli a ve státem uznané svátky |
| `@` | nízkopodlažní vůz (nejde o kalendář, mapuje se na `wheelchair_accessible`) |

### Nástupiště

`platform_code` zůstává, protože `platformId` je v `/connections/{l}/{n}`.
Souřadnice nástupiště **dědí od své stanice** — vlastní upstream nemá.
`stop_id` tedy zůstává `S16P2` a struktura `stops.txt` se nemění; mění se jen
to, že `S1P1` a `S1P2` nově sdílejí bod rodiče.

## Chování při selhání

**CIS nedojede.** Staví se z cache v `data/cis/` a zaloguje se její stáří. Bez
cache build selže celý a poslední `gtfs.zip` se dál servíruje — chybějící spoj
je pro konzumenta k nerozeznání od zrušeného.

**Rejstřík a API se rozejdou.** CIS se aktualizuje po dávkách, API průběžně,
takže drift je otázka času.

Směr *rejstřík má spoj navíc*: `/connections/{l}/{n}` vrátí 404. Jednotlivý
výpadek se přeskočí a započítá; když u jedné linky chybí víc než **5 %** spojů,
build spadne — taková čísla znamenají špatně vybranou verzi, ne zrušené spoje.

Směr *API má spoj navíc* nejde levně zjistit, ale ohlásí se sám: `feed.py` už
dnes počítá vozidla bez odpovídajícího spoje. Z ladicího čítače se stane
budíček na zastaralý rejstřík.

**Podpis vyprší uprostřed crawlu.** 2 700 requestů se do 15 minut nevejde.
Hlavička se počítá při každém volání; na 401/403 se jednou přegeneruje
a požadavek zopakuje.

**Zátěž.** Rychlost 8 req/s, souběžnost 8. Crawl vyjde zhruba na 6 minut.

## Testy

Drží zdejší zvyk — nahrané fixtures, žádná síť.

- `cis/index.py` — ručně psané mini-NeTEx se dvěma verzemi jedné linky; test
  tvrdí, že vyhraje pozdější `FromDate`. Plus regrese na ověřený fakt: verze
  655001 od 2026-07-01 má 206 spojů, starší 283.
- `api/client.py` — rotace podpisu proti zmrazenému času, chování na 401.
- `calendar.py` — písmena `X`/`6`/`+`/`@` na `Service`.
- `feed.py` — parsování `currentDelay` včetně záporných hodnot
  (`-PT1M43S` je 103 s náskok, ne zpoždění).
- Stávající JSON fixtures se přenahrají z nového API; `test_tracker.py` se maže
  s modulem.

## Křížová validace

Poslední krok, a jeho příprava musí být krok **první**: současný
`data/gtfs.zip` je jediná kopie feedu ze starého API (build z 8. 8. 2026)
a migrace by ho přepsala. Odložit ho stranou jako referenci předchází všemu
ostatnímu.

Porovnávají se množiny `route_id`, `trip_id`, `stop_id`, časy v `stop_times`
a přiřazení služeb. Známé a očekávané rozdíly se vypíšou zvlášť a nepočítají
se jako chyba:

- souřadnice nástupišť, které nově dědí od stanice,
- spoje, které se mezi 8. 8. a datem migrace reálně změnily.

Výstupem je zpráva pro člověka, ne test, který spadne.

## Co tenhle návrh neřeší

- Tělo `POST /pardubice/paths` zůstává nerozluštěné. Nepotřebujeme ho.
- `/pardubice/events` je pořád prázdné, stejně jako u starého API.
- `gps_course` nové API nemá o nic víc než staré; směr vozidla se dál počítá
  z jízdního řádu.
- Linky, které API zná a CIS ne, by ve feedu chyběly. Dnes taková není —
  všech 32 se v NeTEx najde — ale hlídá to práh z odstavce o driftu.
