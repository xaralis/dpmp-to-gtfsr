# API za api.mhdonline.cz

Zpětně zjištěná dokumentace neveřejného API, ze kterého tenhle projekt staví
oba feedy. Není oficiální a nikdo ho nikde nepopisuje — všechno níž je odvozené
z pozorování webové aplikace DPMP a ověřené proti reálným odpovědím.

## Co se stalo

DPMP vyměnil backend. `online.dpmp.cz/api`, ze kterého tenhle projekt původně
stavěl, je mrtvý — vrací 404 z nginx. Nahradil ho `api.mhdonline.cz` s
prefixem podle provozovatele (`/pardubice`). Proti starému stavu se změnilo
všechno podstatné:

- **Autentizace.** Statický klíč v těle zmizel. Místo něj rotující podpis
  v hlavičce `X-App-Protocol`: `HMAC-SHA256(seed, floor(unix_ms / 900000))`
  v hexu, platný 15 minut. Seed je `your-public-protocol-seed` — placeholder ze
  šablony, který nechali v produkčním bundlu. Veřejný stejně jako předtím klíč,
  ale v projektu chodí přes proměnnou `DPMP_PROTOCOL_SEED`, ne natvrdo v kódu.
- **Volání.** Všechno `GET`, žádné `Content-Type: text/plain` kouzlo.
- **Zmizel hromadný výpis spojů linky.** Starý `connections?line=X` byl základ
  `trips.txt`, `stop_times.txt` i `calendar.txt`. Náhrada v API neexistuje —
  ověřeno proti jejich vlastnímu JS bundlu, jejich frontend celý jízdní řád
  nikdy nezobrazuje.
- **Zmizel endpoint `codes`.** Význam kódů se musí zadrátovat — ale ne odhadem
  z normy: jejich vlastní aplikace nese celou tabulku v bundlu
  (`online.dpmp.cz/assets/index-*.js`, hledej `description:"zastávka na
  znamení"`). Ověřeno proti ní doslova:

  | kód | význam | úroveň |
  |-----|--------|--------|
  | `X` | jede v pracovních dnech | spoj |
  | `6` | jede v sobotu | spoj |
  | `+` | jede v neděli a ve státem uznané svátky | spoj |
  | `1`–`5`, `7` | jede v pondělí … pátek, v neděli | spoj |
  | `@` | garantovaný nízkopodlažní spoj | spoj |
  | `@` | zastávka je bezbariérově přístupná | zastávka |
  | `x` | zastávka na znamení | zastávka |
  | `J` | zastávka u veřejného letiště | zastávka |

  Číselné kódy `1`–`7` používá jen linka 90 (letištní shuttle), která jezdí ve
  dnech, kdy se létá. Než se doplnily, shodily celý build.

  Tištěný jízdní řád DPMP má vlastní, jinou sadu značek (`Q` bezbariérovost,
  `J` jízdenkový automat) a dny provozu tam nejsou kódy, ale sloupce tabulky.
  Na data z API se ta legenda nevztahuje.
- **Zmizely souřadnice nástupišť.** `/stops` vrací 219 plochých zastávek
  s jedním bodem; nástupiště dědí souřadnice od své stanice.

Naopak přibylo:

- **`currentDelay` je skutečné zpoždění** v ISO-8601 (`"-PT1M43S"`), ne odpočet
  do plánovaného odjezdu jako staré `time_difference`.
- **`connectionId` u vozidla** rozřeší spoj napřímo.
- **`onStation`** říká, jestli vůz stojí v zastávce.

Endpointy, které tenhle projekt používá:

| endpoint | parametry | vrací | používáme na |
|---|---|---|---|
| `stops` | — | zastávky, GPS, `fixedCodes` | `stops.txt` |
| `lines` | — | linky, `jdfId` (JDF číslo linky, jen k identifikaci) | `routes.txt` |
| `connections/{line}/{n}` | linka, číslo spoje | zastávkové časy jednoho spoje | `stop_times.txt` |
| `vehicles` | — | živé polohy vozidel, jedna společná `time` | GTFS-RT |
| `events` | — | mimořádnosti | zatím nic, vždy prázdné — stejně jako u starého API |

Bundl nabízí i další cesty (`configuration/connectionDetail`, `presets`,
`vehicles/movement`, `paths/{a}/{b}`, `stops/{id}/incomingArrivals`,
`connections/{line}/longest`, …), ale žádnou z nich projekt nepotřebuje.

## Zdroj jízdních řádů: dohledávání přes API samotné

Dřív seznam spojů dodával celostátní CIS JŘ (`portal.cisjr.cz`), kam DPMP
jízdní řády odevzdává jako primární zdroj. Tenhle projekt ho používal přes
`dpmp_gtfs.cis`, které stahovalo dva NeTEx archivy a z nich stavělo rejstřík
„která linka má k danému dni která čísla spojů a kterým směrem".

**Proč je pryč.** Změřeno proti živému API: ze 32 linek se 3 rozešly s CIS
rejstříkem (linka 12 o 28,5 %, linka 9 o 15,1 %, linka 3 o 7,1 %), ostatních
29 sedělo přesně. Rozchod nesouvisí se stářím verze — linka 1 i linka 12
běžely na verzi staré 40 dní, první s rozchodem 0 %, druhá 28,5 % — je to
vlastnost jednotlivé linky, ne postupné zastarávání. Celkem 63 z 2 762 spojů
chybělo v feedu, aniž by o tom cokoli řeklo. A prahová hodnota to nespraví:
jen si vybírá mezi hlasitým selháním buildu a tichou dírou v feedu, protože
spoj, o kterém rejstřík neví, se u API nikdy ani nezkusí — nemá číslo, které
by se dalo požádat. Vlastní zdroj dat, který se s API neshoduje a nedá se s
ním sesynchronizovat, je horší než mít jenom API.

**Co ho nahradilo.** Dvě věci, obě jen z `api.mhdonline.cz`:

- **Seznam spojů** dohledá [`static/discovery.py`](../src/dpmp_gtfs/static/discovery.py)
  procházením číselného prostoru spojů linky — `connections/{line}/{n}` pro
  rostoucí `n`, dokud nepřijde dost po sobě jdoucích 404. Mezery v číslování
  jsou v síti běžné (`n` je JDF číslo spoje, ne pořadí), ale nejdelší naměřená
  mezera je 18, takže hranice 50 po sobě jdoucích chyb má bezpečnou rezervu.
- **`direction_id`** dopočítá [`static/direction.py`](../src/dpmp_gtfs/static/direction.py)
  z pořadí zastávek: dva spoje sdílející aspoň dvě zastávky buď jedou ve
  stejném relativním pořadí (stejný směr), nebo v obráceném (opačný) — nic
  třetího, dokud srovnání nevyjde nejednoznačně. To dá přesný vztah
  „stejný/opačný" mezi každou dvojicí spojů linky, který se dvoubarevně
  prochází jako graf; žádný representativní spoj, žádná většina, žádné
  hádání terminálů.

  Jediná skutečná nejednoznačnost jsou okružní linky: pokud se graf rozpadne
  na víc souvislých komponent (typicky proto, že některé spoje sdílejí míň
  než dvě zastávky se zbytkem), nemá relativní značení *mezi* komponentami
  žádný geometrický podklad — kód to loguje a přijímá, nesnaží se hádat.

**Co dohledávání nedá:** souřadnice nástupišť pořád nemá nikdo — ty dál
zůstávají po zastávce, ne po nástupišti, viz výše.

## Dny provozu: jediná věc, kterou API neumí

CIS je v projektu zpátky, ale **výhradně jako zdroj dnů provozu**. Rejstřík
spojů z něj nebereme dál — důvody výš platí beze změny.

**Proč.** `fixedCodes` u zhruba třetiny spojů neodpovídají jízdnímu řádu, který
DPMP samo vyvěšuje. Doložený případ: spoj 46 linky 1, 06:36 ze Slovany,točna.
Vyvěšený jízdní řád i CIS říkají *pracovní den*, API posílá kód `+`, tedy
neděle a svátky. U linky 1 sedí staré API s CIS na 206 z 206 spojů, nové na
163. Chyba je v hodnotách, ne v našem čtení: tabulka kódů níž je opsaná
doslova z bundlu oficiální aplikace DPMP. Hlášeno dopravnímu podniku.

**Jak.** [`cis/calendars.py`](../src/dpmp_gtfs/cis/calendars.py) čte z NeTEx
denní bitmapu (`UicOperatingPeriod/ValidDayBits`) a
[`static/calendar.py`](../src/dpmp_gtfs/static/calendar.py) z ní udělá týdenní
vzorec plus výjimky. Bitmapa je nutná, ne přepych: DPMP jezdí tři různé
varianty pracovních dnů podle školního vyučování a do sedmi sloupců
`calendar.txt` se to nevejde.

Párování je `(jdf_id linky, číslo spoje)` ↔ `ServiceJourney/Name`, ověřené
nezávisle časy prvního odjezdu. 63 z 2 762 spojů (2,3 %) v CIS protějšek nemá —
soustředěně na linkách 12, 9 a 3 — a těm zůstanou kódy z API; build to loguje
po spojích a přeleze-li podíl 10 %, hlásí to jako chybu.

## Na co si dát pozor

Podrobněji i s důkazy v [`upstream.py`](../src/dpmp_gtfs/upstream.py) a
[`api/models.py`](../src/dpmp_gtfs/api/models.py).

- **Čas snímku je teď jeden na celou odpověď `/vehicles`**, ne u každého
  vozidla zvlášť. Staré `state_dtime` u jednotlivého vozidla neexistuje;
  odpověď nese jedno pole `time`, v UTC se sufixem `Z`.
- **`gps_course` v novém API neexistuje o nic víc než ve starém.** Směr
  vozidla se proto dál počítá z jízdního řádu, ne z kompasu vozu.
- **`/events` je stále vždy prázdné**, přesně jako u starého API. Element
  toho, co by tam mělo být, tak zůstává neznámý.
- **Souřadnice zastávky jsou nepovinné.** Jedna zastávka (`id` 147,
  „Opočínek,rozvodna") je publikovaná úplně bez souřadnic. `Stop.gps_latitude`
  a `gps_longitude` proto mají výchozí hodnotu `None` místo povinného pole, a
  builder takové zastávky z feedu přeskočí, aby neshodily validaci celého
  `/stops`.
- **Chybějící zpoždění a nerozebratelné zpoždění jsou dvě různé věci** — to je
  úmyslné produktové rozhodnutí, ne opomenutí:
  - `currentDelay` chybějící nebo `null` znamená, že se pro to vozidlo
    nepublikuje žádný `TripUpdate` vůbec. Chybějící údaj není nula — publikovat
    nulu by tvrdilo přesnost tam, kde upstream nic neřekl.
  - `currentDelay` přítomné, ale nerozparsovatelné jako ISO-8601 durace, se
    zaloguje jako varování a spočítá jako nulové zpoždění — spoj se tedy dál
    publikuje, jen s `delay == 0`, protože je lepší mít spoj v feedu s nulou
    než ho kvůli jedné špatné hodnotě ztratit celý.

### Past na velikost písmen

`fixedCodes` existují na dvou úrovních a stejné písmeno v nich znamená různé
věci:

| úroveň | kód | význam |
|---|---|---|
| spoj (`/connections/{line}/{n}`) | `X` | jede v pracovních dnech |
| spoj | `6` / `+` | sobota / neděle a svátky |
| spoj | `@` | nízkopodlažní vůz |
| zastávka (`/stops`) | `@` | bezbariérová zastávka (`wheelchair_boarding`) |
| zastávka | `x` | zastávka na znamení |

Velké `X` a malé `x` jsou dva různé kódy. Porovnání kódů musí být
case-sensitive.

### Pole, která v `/connections` zmizela

Starý `ConnectionStop` nesl `index`, `distance` a per-zastávkové `codes`. Nový
vrací jen `stopId`, `platformId` a `departureTime`/`arrivalTime`. Dopad:

- **`index` živil směr spoje.** Náhrada je popsaná výše: `direction_id` se
  dopočítá z pořadí zastávek napříč spoji stejné linky, ne z pole, které API
  publikuje.
- **`distance`** byl už dřív příliš hrubý na `shape_dist_traveled`. Nechybí.
- **per-zastávkové `codes`** živily „na znamení". Náhrada je `fixedCodes` na
  zastávce v `/stops` — sémantika se mírně mění, „na znamení" je nově
  vlastnost zastávky, ne zastávky v rámci konkrétního spoje. To je spíš
  správnější.
