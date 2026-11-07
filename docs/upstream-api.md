# API za api.mhdonline.cz

Zpětně zjištěná dokumentace neveřejného API, ze kterého tenhle projekt bere
zastávky a živé polohy vozů. Není oficiální a nikdo ho nikde nepopisuje —
všechno níž je odvozené z pozorování webové aplikace DPMP a ověřené proti
reálným odpovědím. Jízdní řád z něj od listopadu 2026 nebereme, viz další
kapitola.

## 403 na `connections`: jízdní řád z API číst nejde

Od 25. 8. 2026 padala noční přestavba na `403` na `connections/1/1`. Změřeno
7. 11. 2026:

- **Podpis se nezměnil.** Bundl `online.dpmp.cz/assets/index-*.js` počítá
  `X-App-Protocol` pořád stejně a `stops`, `lines`, `vehicles` i
  `connections/{line}/longest` na něj odpovídají 200. Bez podpisu je 403 všude.
- **`connections/{line}/{n}` odpovídá jen pro spoj, na kterém zrovna jede vůz.**
  Z 56 po sobě jdoucích čísel linky 1 dala 200 právě ta tři, která v tu chvíli
  hlásilo `/vehicles`, ostatních 53 dalo 403 s prázdným tělem — i čísla, která
  neexistují (`connections/1/99999`). Neexistující linka je 404.
- **Není to blokace nás.** Stejná odpověď přijde z jejich vlastní stránky
  v prohlížeči, se stejnou hlavičkou. Jejich aplikace se na jiný spoj než na ten
  rozkliknutého vozu nikdy neptá, takže si toho nevšimne.

Je to tedy pravidlo pro všechny, ne chyba ani mechanismus, který by se dal
odvodit a napodobit. **Neobcházíme ho** a spoje si postupně nesbíráme z jedoucích
vozů: celý jízdní řád bereme z veřejného registru CIS JŘ, kam ho DPMP odevzdává.

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
| `vehicles` | — | živé polohy vozidel, jedna společná `time` | GTFS-RT |
| `events` | — | mimořádnosti | zatím nic, vždy prázdné — stejně jako u starého API |

Bundl nabízí i další cesty (`configuration/connectionDetail`, `presets`,
`vehicles/movement`, `paths/{a}/{b}`, `stops/{id}/incomingArrivals`,
`connections/{line}/longest`, …), ale žádnou z nich projekt nepotřebuje.

## Jízdní řád: z CIS

[`cis/timetable.py`](../src/dpmp_gtfs/cis/timetable.py) čte z NeTEx archivů CIS
všechno: spoje, časy, směr (`DirectionRef`), nízkopodlažnost, „na znamení" a dny
provozu. [`static/timetable.py`](../src/dpmp_gtfs/static/timetable.py) to posadí
na zastávky z `/stops`. Celá přestavba jsou dva dotazy na API a pár set MB
podmíněně stažených archivů, patnáct sekund práce místo dvaceti minut a 4 400
dotazů.

- **Zastávky se párují podle jména.** Z 218 jmen v CIS se od `/stops` liší jen
  čárkami, mezerami a velikostí písmen (`Slovany,U Přejezdu` proti
  `Slovany,u přejezdu`, `ERA,Zámeček` proti `ERA Zámeček`). Neznámé jméno build
  zaloguje a zastávku ze spoje vypustí.
- **Linky rozhoduje `/lines`.** CIS má navíc 902 a 906, které DPMP ve svém seznamu
  nemá; publikované by zdvojily odjezdy jiných linek. Linku 910 nemá ani jeden
  zdroj, na dpmp.cz je stránka bez jízdního řádu.
- **Nástupiště CIS nemá.** Zastávky jsou proto ve feedu bez nástupišť, `S16`
  s `location_type` 0, a poloha se tím nemění: `/stops` stejně dává jeden bod na
  stanici. Čísla nástupišť z API zmizela s přístupem k `connections`.
- **Dny provozu** jsou denní bitmapa (`UicOperatingPeriod/ValidDayBits`), ze které
  [`static/calendar.py`](../src/dpmp_gtfs/static/calendar.py) udělá týdenní vzorec
  plus výjimky. Je nutná: DPMP jezdí tři varianty pracovních dnů podle školního
  vyučování a prázdninové dny neplynou z data.

### Čísla spojů API a CIS nejsou totéž

`connectionId` z `/vehicles` se s číslem v CIS často shoduje, ale ne vždy: ze 28
vozů 7. 11. 2026 jich 9 hlásilo číslo, které je v CIS jiná jízda téže linky. Vůz
na lince 1 hlásí spoj 383 s odjezdem z Jesničánek ve 20:41, tutéž jízdu API vrací
pod 383 a CIS vede jako 397, kdežto 383 je v CIS jízda o hodinu dřív.

Realtime proto číslu nevěří: [`StaticIndex.trip_of`](../src/dpmp_gtfs/realtime/index.py)
pozná spoj podle toho, že dnes jede a staví na příští zastávce vozu v čas, kdy tam
vůz hlásí být. Číslo rozhoduje jen shodu. Radši vůz bez spoje než vůz na cizím
spoji. Ověřeno proti `connections` jedoucích vozů: 26 z 26 stejných časů, všech 28
přiřazeno.

**Hlášení o `fixedCodes` z 10. 8. 2026 je tím pravděpodobně špatně.** Tvrzení „kódy
nesedí u třetiny spojů" vzniklo spojením API a CIS přes číslo spoje, takže část
rozdílů bude jen tohle. Není to doložené spoj po spoji, protože `connections` už
na to neodpoví, ale do DPMP by to chtělo poslat opravu.

### Verze jedné linky se nepřebíjejí, navazují na sebe

Linka je v archivu vícekrát: celoroční verze plus doplněk na dobu školních
prázdnin nebo výluky. K datu buildu jich platí víc, ale nesoupeří o celý rok —
**dělí si časovou osu**. Verzi proto vybíráme **pro každý den zvlášť**: vyhrává
pozdější `FromDate`, při shodě kratší platnost (dočasná verze položená přes tu
dlouhou; na lince 21 od 1. 9. obě a vozy jezdí tu kratší).

Číslo spoje **není napříč verzemi identifikátor**. Změřeno na lince 2: zářijová
verze sdílí se srpnovou 103 čísel spojů a u 45 z nich znamená jinou jízdu. Spoj
se proto protáhne přes změnu, jen když se **shodují všechny jeho zastávky a časy**.
Jinak má pozdější jízda spoj sám pro sebe s datem své verze v čísle
(`L3C446-20261114`). Feed tak zná jízdní řád i za změnou a po ní se jen přejmenuje
na čisté číslo.

To byla příčina dřívějšího stavu „17 služeb končí 31. 8.": spoje další verze ve
feedu nebyly vůbec, spoléhalo se na noční přestavbu, a ta neběžela.

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
| spoj | `X` | jede v pracovních dnech |
| spoj | `6` / `+` | sobota / neděle a svátky |
| spoj | `@` | nízkopodlažní vůz |
| zastávka (`/stops`) | `@` | bezbariérová zastávka (`wheelchair_boarding`) |
| zastávka | `x` | zastávka na znamení |

Velké `X` a malé `x` jsou dva různé kódy. Porovnání kódů musí být
case-sensitive.
