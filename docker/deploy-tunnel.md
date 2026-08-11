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

Celé z terminálu, bez klikání v dashboardu. Routování zůstává v repozitáři,
takže změna hostname je dohledatelný commit, ne nezaznamenané kliknutí.

**1. Vytvoř tunel a nasměruj na něj DNS** (jednou, lokálně).

```bash
cloudflared tunnel login            # otevře prohlížeč, vyber doménu
cloudflared tunnel create dpmp-gtfs
cloudflared tunnel route dns dpmp-gtfs gtfs.progresivni-pardubice.cz
```

`create` vypíše ID tunelu a uloží credentials do
`~/.cloudflared/<ID>.json`. `route dns` vytvoří CNAME — žádný A záznam ani
certifikát řešit netřeba.

**2. Přenes to na server.**

```bash
scp -r ~/Workspace/dpmp-to-gtfsr ubuntu@IP:/opt/dpmp-gtfs
scp ~/.cloudflared/<ID>.json ubuntu@IP:/opt/dpmp-gtfs/docker/cloudflared/credentials.json
```

**3. Nastav a spusť.**

```bash
ssh ubuntu@IP
cd /opt/dpmp-gtfs

cat > .env <<'ENV'
TUNNEL_ID=sem-vloz-id-tunelu
TUNNEL_HOSTNAME=gtfs.progresivni-pardubice.cz
ENV
chmod 600 .env

./docker/tunnel-up.sh
```

Skript vygeneruje konfiguraci tunelu ze šablony a spustí stack. Před tím
zkontroluje, že `.env` i credentials existují a že `TUNNEL_ID` souhlasí
s tím, co je v credentials — nesoulad by jinak vyrobil tunel, který se
připojí a pak neobsluhuje nic, což se z logu čte špatně.

Žádný klíč se nenastavuje. Nové API se autentizuje podpisem, který se počítá
ze seedu a mění se po patnácti minutách; seed je veřejný a má rozumnou výchozí
hodnotu v `config.py`. Přepsat ho jde proměnnou `DPMP_PROTOCOL_SEED`, kdyby ho
DPMP vyměnil.

Hotovo. Certifikát vydá Cloudflare sám.

`/healthz` odpoví hned; první jízdní řád ale chvíli trvá a služba do té doby
hlásí, že data teprve stahuje (vidět je to i na mapě). Počítej zhruba
s **25 minutami**: stáhnou se archivy CIS (~300 MB, podruhé už jen kontrola,
jestli se změnily), projde se ~4 400 dotazů na API a doroutuje se geometrie
tras. Archivy i geometrie zůstávají na svazku, takže restart je otázka minut:

```bash
docker compose --env-file .env -f docker/compose.tunnel.yaml logs -f
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
docker compose --env-file .env -f docker/compose.tunnel.yaml exec feed \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"

# ruční přestavba jízdních řádů (jinak běží sama v noci)
docker compose --env-file .env -f docker/compose.tunnel.yaml exec feed dpmp-gtfs build-static

# zastávky, které přišly o obsluhu (obvykle výluka)
docker compose --env-file .env -f docker/compose.tunnel.yaml logs feed | grep -i "lost all service"
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
