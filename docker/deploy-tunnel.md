# Nasazení přes Cloudflare Tunnel

Nejjednodušší způsob, jak tuhle službu dostat na veřejnou adresu na stroji,
kde už něco běží.

Tunel se připojuje **ven** k Cloudflare, takže nasazení nepotřebuje volný port,
DNS A záznam, certbot ani zásah do čehokoli, co na stroji už je. Konkrétně:
nesahá na nginx orchestratoru, nemění jeho `docker-compose.prod.yml` a
nepokládá nic do jeho adresářů. Orchestrator o téhle službě neví a jeho vlastní
deploy ji nemůže rozbít — a naopak.

Podmínka je jediná: doména musí být v Cloudflare. `progresivni-pardubice.cz`
tam je, takže třeba `gtfs.progresivni-pardubice.cz`.

## Postup

**1. Vytvoř tunel a zkopíruj token.**

V [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Networks** →
**Tunnels** → *Create a tunnel* → **Cloudflared** → pojmenuj `dpmp-gtfs`.

Na další obrazovce Cloudflare nabídne instalační příkazy — ty ignoruj, zajímá
tě jen **token** (dlouhý řetězec za `--token`).

**2. Nastav veřejný hostname.**

Ve stejném průvodci, záložka *Public Hostnames* → *Add a public hostname*:

| pole | hodnota |
|---|---|
| Subdomain | `gtfs` |
| Domain | `progresivni-pardubice.cz` |
| Service type | `HTTP` |
| URL | `feed:8000` |

`feed` je název služby z compose souboru; tunel a feed sdílí compose síť, takže
se najdou podle jména.

**3. Spusť to.**

```bash
scp -r ~/Workspace/dpmp-to-gtfsr ubuntu@IP:/opt/dpmp-gtfs
ssh ubuntu@IP
cd /opt/dpmp-gtfs

cat > .env <<'ENV'
DPMP_API_KEY=sem-vloz-klic
TUNNEL_TOKEN=sem-vloz-token-z-kroku-1
ENV
chmod 600 .env

./docker/tunnel-up.sh
```

Skript vygeneruje konfiguraci tunelu ze šablony a spustí stack. Před tím
zkontroluje, že `.env` i credentials existují a že `TUNNEL_ID` souhlasí
s tím, co je v credentials — nesoulad by jinak vyrobil tunel, který se
připojí a pak neobsluhuje nic, což se z logu čte špatně.

`DPMP_API_KEY` je klíč, který veřejná aplikace DPMP posílá ze svého JS bundlu
(`online.dpmp.cz`, chunk `pages/lines-*.js`, hledej `key`). Není to tajemství,
ale do repozitáře nepatří — jeho výměna má být restart, ne commit.

Hotovo. DNS záznam vytvoří Cloudflare sám, certifikát taky.

První start prochází celý jízdní řád a routuje geometrii tras, takže než
`/healthz` odpoví 200, počítej zhruba se **7 minutami**:

```bash
docker compose -f docker/compose.tunnel.yaml logs -f
```

## Aktualizace

```bash
cd /opt/dpmp-gtfs && git pull && ./docker/tunnel-up.sh
```

Volume `feed-data` zůstává, takže restart trvá sekundy — feed se načte z disku
a nemusí se stavět znovu.

## Provoz

```bash
# stav
docker compose -f docker/compose.tunnel.yaml exec feed \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"

# ruční přestavba jízdních řádů (jinak běží sama v noci)
docker compose -f docker/compose.tunnel.yaml exec feed dpmp-gtfs build-static

# zastávky, které přišly o obsluhu (obvykle výluka)
docker compose -f docker/compose.tunnel.yaml logs feed | grep -i "lost all service"
```

## HTTPS

Certifikát vydává a obnovuje Cloudflare, takže `https://` funguje hned a nic
se kolem něj nekonfiguruje. Dvě věci ale stojí za zapnutí v dashboardu
(**SSL/TLS → Edge Certificates**):

- **Always Use HTTPS** — přesměruje případný `http://` požadavek. Bez toho
  je HTTP dostupné taky.
- **HSTS** — až si ověříš, že vše na HTTPS jede. Zapíná se snadno, vypíná
  špatně (prohlížeče si hlavičku pamatují měsíce), takže až nakonec.

Režim SSL/TLS nech na **Full** nebo **Flexible**; spojení mezi Cloudflare
a strojem drží tunel, ne certifikát na originu.

Compose spouští uvicorn s `--proxy-headers --forwarded-allow-ips=*`, aby
aplikace viděla původní schéma a IP klienta. Výchozí nastavení by hlavičky
zahodilo, protože nepřicházejí z localhostu, ale z kontejneru s tunelem.
Důvěřovat jim je tu bezpečné právě proto, že port není nikam publikovaný —
jediné, co na službu dosáhne, je tunel.

## Na co si dát pozor

Cloudflare cachuje podle hlaviček, které aplikace posílá sama, takže default
je v pořádku. Jen **nezapínej agresivní cache na `/gtfs-rt.pb`** — patnácti­
sekundový feed držený hodinu je horší než žádný.

Tunel drží spojení sám a po restartu stroje naběhne s Dockerem
(`restart: unless-stopped`). Když Cloudflare hlásí tunel jako *Down*, podívej
se do logu služby `tunnel`.

## Alternativa: vlastní stroj

Pokud by ta služba jednou měla mít vlastní instanci, [compose.yaml](compose.yaml)
a [deploy-lightsail.md](deploy-lightsail.md) popisují klasické nasazení
s vlastním nginx a certbotem. Pro přidání na už obsazený stroj je ale tunel
jednodušší i bezpečnější.
