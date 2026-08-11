# Záznamy zaniklého API

Nic z toho nepoužívají testy. Je to poslední zachovaný otisk starého
rozhraní DPMP, které bylo 10. srpna 2026 vypnuto a už nikdy nepůjde
dotázat znovu.

Drží se to tu z jediného důvodu: opíráme se o ně v tvrzení, že **starší API
vracelo dny provozu správně** a nové ne. To tvrzení je adresované
dopravnímu podniku (viz [`docs/dpmp-hlaseni-kalendare.md`](../../../docs/dpmp-hlaseni-kalendare.md)),
takže musí zůstat doložitelné i za rok, kdy si na okolnosti nikdo
nevzpomene.

| soubor | co dokládá |
|---|---|
| `codes.json` | vlastní legendu starého API: `X` = jede v pracovních dnech, `x` = zastávka na znamení, `@` = nízkopodlažní spoj i bezbariérová zastávka |
| `connections-1.json`, `connections-2.json` | kódy všech spojů linek 1 a 2, jak je vracelo staré API |
| `gtfs-old-api.zip` | poslední feed postavený ze starého API, 2 728 spojů |

Reprodukce klíčového měření: kódy z `connections-{1,2}.json` přeložené přes
`codes.json` dávají u 417 ze 417 spojů tentýž kalendář, jaký nese
`gtfs-old-api.zip`, a ten se u linky 1 shoduje s CIS u všech 206 spojů.
Nové API se s CIS shoduje u 163.

**Neupravovat.** Znovu je pořídit nelze.
