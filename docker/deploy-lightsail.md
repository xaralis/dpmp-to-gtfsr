# Nasazení na vlastní instanci

Tenhle postup platí pro stroj, kde služba běží **sama** a může si vzít porty
80 a 443. Pokud na stroji už něco jede, použij místo toho
[Cloudflare Tunnel](deploy-tunnel.md) — je jednodušší a nesahá na nic
existujícího.

## Proč instance, a ne Container Service

Lightsail Container Service nemá persistentní disk. Bez něj by se při každém
restartu i nasazení znovu procházel celý jízdní řád — 2 760 volání a zhruba
tři minuty, než služba vůbec začne odpovídat. Naměřeno na hotovém obrazu:

| start | doba do `healthz: 200` |
|---|---|
| studený, prázdný disk | 165 s |
| s feedem na disku | 5 s |

Proto běžná instance s Dockerem a pojmenovaným volume.

## Instance

Stačí nejmenší nabízený plán. Služba drží jízdní řád v paměti (~2 700 spojů)
a jednou za 15 s zpracuje jeden HTTP dotaz; obraz má 208 MB.

1. Vytvoř instanci **Linux/Unix → OS Only → Debian 12**.
2. V *Networking* povol příchozí **HTTP (80)** a **HTTPS (443)**. Port 8000
   nech zavřený — chodí se přes reverzní proxy.
3. Přiřaď **statickou IP**, jinak se po restartu změní.

## Instalace

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER   # po tomhle se odhlas a přihlas znovu
```

```bash
git clone https://github.com/xaralis/dpmp-to-gtfsr.git
cd dpmp-to-gtfsr
```

Klíč do `.env` vedle `compose.yaml`:

```bash
echo 'DPMP_API_KEY=...' > docker/.env
```

Klíč je ten, který veřejná aplikace DPMP posílá ze svého JS bundlu. Není to
tajemství, ale do gitu nepatří — jeho výměna má být restart, ne commit.

```bash
docker compose -f docker/compose.yaml up -d
docker compose -f docker/compose.yaml logs -f
```

První start staví feed od nuly, počítej s ~3 minutami do prvního `healthz: 200`.

## Reverzní proxy a TLS

Caddy zařídí certifikáty sám:

```bash
sudo apt-get install -y caddy
```

`/etc/caddy/Caddyfile`:

```
gtfs.example.cz {
    reverse_proxy localhost:8000
}
```

```bash
sudo systemctl reload caddy
```

Cache hlavičky si nastavuje aplikace sama (`ETag` na `gtfs.zip`,
`max-age=15` na `gtfs-rt.pb`), takže proxy do nich nemusí sahat.

## Cloudflare (volitelně)

Doména může viset na Cloudflare v proxy režimu. Odlehčí to origin a přidá
globální cache. Pak je ale potřeba:

- **nezapínat** agresivní cache na `/gtfs-rt.pb` — patnáctisekundový feed,
  který Cloudflare drží hodinu, je horší než žádný,
- respektovat `Cache-Control` z originu (výchozí chování).

Cloudflare Pages jako jediný hosting nepřipadá v úvahu: běžící Python službu
neuhostí a limit subrequestů by neunesl crawl jízdního řádu.

## Provoz

```bash
# stav
curl -s localhost:8000/healthz | jq

# ruční přestavba jízdních řádů (jinak běží sama v noci)
docker compose -f docker/compose.yaml exec feed dpmp-gtfs build-static

# aktualizace
git pull && docker compose -f docker/compose.yaml up -d --build
```

`/healthz` vrací 503, jakmile je realtime feed starší než dvě minuty — hodí se
jako cíl externího monitoringu.

Za sledování stojí i varování v logu o zastávkách, které přišly o obsluhu:
obvykle znamenají začátek nebo konec výluky.

```bash
docker compose -f docker/compose.yaml logs | grep -i "lost all service"
```

## Zálohy

Zálohovat není co. Volume drží jen vygenerovaný feed, který jde kdykoli
postavit znovu z API; cena je oněch 165 sekund.
