# dpmp-to-gtfsr

GTFS a GTFS-Realtime feed pro pardubickou MHD.

Dopravní podnik města Pardubic provozuje [online.dpmp.cz](https://online.dpmp.cz/), která ukazuje
polohu spojů v reálném čase, ale nepublikuje žádný export v průmyslovém standardu. Bez něj se
pardubická MHD neobjeví v Google Maps, Mapy.cz, Transitous ani v žádné analytice.

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

## Provoz

```bash
echo 'DPMP_API_KEY=...' > docker/.env
docker compose -f docker/compose.yaml up -d
```

První start postaví feed od nuly (~3 min), další starty ho načtou z volume (~5 s).
Podrobný postup nasazení včetně TLS: [docker/deploy-lightsail.md](docker/deploy-lightsail.md).

Konfigurace přes proměnné prostředí s prefixem `DPMP_`, viz
[`config.py`](src/dpmp_gtfs/config.py). Povinná je jediná — `DPMP_API_KEY`.
Klíč není tajemství, veřejná aplikace DPMP ho má napevno ve svém JS bundlu, ale v repozitáři
přesto není, aby jeho výměna neznamenala změnu kódu.

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
