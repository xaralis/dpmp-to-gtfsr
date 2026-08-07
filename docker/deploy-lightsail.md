# Nasazení na AWS Lightsail

## Vedle už běžící služby (varecha.work)

Pokud na instanci **už běží orchestrator**, platí tenhle zkrácený postup. Ten
si drží nginx na portech 80/443 a certbot renewal smyčku, takže tahle služba
nesmí publikovat vlastní porty — připojí se na sdílenou Docker síť a nginx na
ni bude proxovat podle jména kontejneru.

Předpokládá se `IP` = `pulumi stack output public_ip` ve `~/Workspace/orchestrator/infra`.

**1. DNS.** V Porkbunu přidej A záznam `gtfs.varecha.work` → stejná IP.

**2. Sdílená síť a soubory.**

```bash
ssh ubuntu@IP 'docker network create shared 2>/dev/null; mkdir -p /opt/dpmp-gtfs'
scp docker/compose.behind-proxy.yaml ubuntu@IP:/opt/dpmp-gtfs/
scp docker/nginx-gtfs.conf ubuntu@IP:/opt/orchestrator/nginx/gtfs.conf
ssh ubuntu@IP 'echo "DPMP_API_KEY=3e86570d-56a1-4ec1-8012-c1a9f98d18cc" > /opt/dpmp-gtfs/.env'
```

**3. Připoj nginx orchestratoru na sdílenou síť.** V
`/opt/orchestrator/docker-compose.prod.yml` přidej ke službě `nginx`:

```yaml
    networks:
      - default
      - shared
    volumes:
      # ...ke stávajícím:
      - ./nginx/gtfs.conf:/etc/nginx/conf.d/gtfs.conf:ro
```

a na konec souboru:

```yaml
networks:
  shared:
    external: true
```

**4. Certifikát.** Musí se vydat dřív, než nginx načte `gtfs.conf` — jinak
spadne na chybějícím souboru s certifikátem. Proto se konfigurace dočasně
odloží stranou:

```bash
ssh ubuntu@IP "cd /opt/orchestrator && mv nginx/gtfs.conf nginx/gtfs.conf.off && \
  docker compose -f docker-compose.prod.yml restart nginx && \
  docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot \
    certonly --webroot -w /var/www/certbot -d gtfs.varecha.work \
    --non-interactive --agree-tos -m filip.varecha@gmail.com"
```

**5. Spusť feed a zapni nginx blok.**

```bash
ssh ubuntu@IP "cd /opt/dpmp-gtfs && docker compose -f compose.behind-proxy.yaml up -d --build"
ssh ubuntu@IP "cd /opt/orchestrator && mv nginx/gtfs.conf.off nginx/gtfs.conf && \
  docker compose -f docker-compose.prod.yml up -d nginx"
```

První start staví feed od nuly včetně geometrie tras — než odpoví
`healthz: 200`, počítej zhruba s **7 minutami**. Průběh:

```bash
ssh ubuntu@IP 'cd /opt/dpmp-gtfs && docker compose -f compose.behind-proxy.yaml logs -f'
```

Pak je na <https://gtfs.varecha.work> a certifikát se obnovuje stejnou smyčkou
jako u orchestratoru — nic dalšího nastavovat netřeba.

### Aktualizace

```bash
ssh ubuntu@IP "cd /opt/dpmp-gtfs && git -C /opt/dpmp-gtfs/src pull && \
  docker compose -f compose.behind-proxy.yaml up -d --build"
```

Volume `feed-data` přežije, takže restart trvá sekundy, ne minuty.

### Cloudflare doména progpce

Šla by taky — stačí CNAME na `gtfs.varecha.work`. Ale doména na Lightsailu už
má hotovou TLS smyčku, takže vlastní doména je jednodušší a bez další
závislosti. Pokud by to jednou mělo jít přes Cloudflare v proxy režimu, jen
nenastavuj agresivní cache na `/gtfs-rt.pb`: patnáctisekundový feed držený
hodinu je horší než žádný.

---

## Od nuly na čistou instanci

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
