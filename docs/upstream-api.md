# API za online.dpmp.cz

Zpětně zjištěná dokumentace neveřejného API, ze kterého tenhle projekt staví
oba feedy. Není oficiální a DPMP ho nikde nepopisuje — všechno níž je odvozené
z pozorování jejich webové aplikace a ověřené proti reálným odpovědím.

## Volání

Vše je `POST` na `https://online.dpmp.cz/api/<endpoint>`, tělo `{"key": "<uuid>"}`.

**Content-Type musí být `text/plain`.** S `application/json` server vrací 500.
Jejich aplikace na to narazí náhodou: `fetch` s prostým řetězcem v těle posílá
`text/plain` sám od sebe.

Klíč je natvrdo v jejich JS bundlu (`_next/static/chunks/pages/lines-*.js`),
takže to není tajemství — v tomhle projektu ale stejně chodí přes proměnnou
`DPMP_API_KEY`, aby jeho výměna byla restart, ne commit.

Server je citlivý na zátěž: při osmi paralelních spojeních spadlo volání na
timeout. Klient drží konkurenci na čtyřech a mezi požadavky pauzuje.

## Endpointy

| endpoint | parametry | vrací | používáme na |
|---|---|---|---|
| `codes` | — | významy číselných kódů | kalendář, příznaky zastávek |
| `stations` | — | 216 stanic vč. nástupišť a GPS | `stops.txt` |
| `lines` | — | 31 linek se seznamem zastávek | `routes.txt` |
| `connections` | `line` | všechny spoje linky | `trips.txt`, `calendar.txt` |
| `connectionDetail` | `line`, `number` | zastávkové časy spoje | `stop_times.txt` |
| `buses` | — | živé polohy vozidel | GTFS-RT |
| `route` | `line` | **geometrie trasy linky** | zatím nic — viz níž |
| `busConnectionDetail` | `line`, `number` | detail spoje, jiný tvar | nepoužíváme |
| `events` | — | mimořádnosti | zatím nic, vždy prázdné |
| `currentConnections` | `line` | právě jedoucí spoje | nepoužíváme, redundantní |

### `route` — geometrie linky

Objeveno až v srpnu 2026, protože se volá teprve po kliknutí na „Detail spoje";
při načtení stránky se neobjeví. Průzkum postavený na tom, co je vidět v síti
po otevření webu, ho systematicky mine.

```json
{
  "line_number": 1,
  "route": [[49.9899994, 15.7752138], [49.9895062, 15.7755732], ...],
  "stations": [179, 178, 177, 222, 1, 15, ...]
}
```

`route` je posloupnost `[šířka, délka]` kopírující skutečné ulice. Funguje pro
**všech 31 linek**, medián kroku 11–21 m; nejdelší je linka 99 s 1080 body přes
27 km.

**Není to geometrie spoje.** `stations` je sjednocení zastávek linky přes oba
směry, takže neodpovídá žádné konkrétní sekvenci zastávek: u linky 1 (26 stanic)
ani u linky 8 (41 stanic) se netrefí ani jeden spoj. Většina spojů je ale
podmnožinou — 189 z 206 u linky 1, 120 ze 120 u linky 8.

Pro `shapes.txt` by se tedy musela sekvence zastávek na tuhle polyline napasovat
a vyříznout příslušný úsek. Dnes se místo toho routuje přes Valhallu nad OSM
(viz [`shapes.py`](../src/dpmp_gtfs/static/shapes.py)); geometrie od
provozovatele by byla přesnější a odpadla by závislost na komunitním routeru.

### `busConnectionDetail`

Vrací totéž co `connectionDetail`, ale zabalené jinak — přidává `codes`,
`departureTime` a `arrivalTime` na úrovni spoje:

```json
{"line_number": 1, "number": 411, "codes": [...], "departureTime": "2141",
 "arrivalTime": "2156", "stops": [{"number": 1, "name": "Jesničánky,točna",
 "index": 4, "distance": 0, "arrivalTime": "", "departureTime": "2141",
 "platform": 1}, ...]}
```

## Na co si dát pozor

Podrobněji i s důkazy v [`upstream.py`](../src/dpmp_gtfs/upstream.py).

- **`state_dtime` je v UTC**, zatímco všechny jízdní řády jsou v lokálním čase.
  Ověřeno proti hlavičce `Date` serveru.
- **`time_difference` není zpoždění**, ale odpočet do plánovaného odjezdu z
  `current_stop`. Klesá o sekundu za sekundu i u naprosto přesného vozu. Zhruba
  42 % vozidel ho má `null` (stojí na výchozí zastávce).
- **`current_stop_number` = stanice × 100 + nástupiště**, kdežto
  `last_stop_number` je holé číslo stanice bez nástupiště. Kvůli tomu se u
  spojů, které stanici obslouží dvakrát, nedá z `last_stop_number` poznat, o
  který průjezd jde.
- **`gps_course` je vždy `null`** — ve všech nahraných snímcích, u všech
  vozidel. Směr vozidla se proto počítá z jízdního řádu.
- **Číselná pole jsou řetězce** a nic nezaručuje, že obsahují čísla.
- **`arrivalTime` je vyplněný jen u poslední zastávky** spoje; jinde platí
  `departureTime`.
- **`distance` jsou celé kilometry** — příliš hrubé na `shape_dist_traveled`.
