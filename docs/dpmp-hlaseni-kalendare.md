# Hlášení: `fixedCodes` v api.mhdonline.cz neodpovídají publikovanému jízdnímu řádu

**Datum:** 10. 8. 2026
**Týká se:** `https://api.mhdonline.cz/pardubice/connections/{linka}/{spoj}`, pole `fixedCodes`
**Dopad:** kódy dnů provozu neodpovídají skutečnosti přibližně u třetiny spojů

## Shrnutí

Pole `fixedCodes` u spoje má podle vaší vlastní aplikace nést mimo jiné dny,
kdy spoj jede (`X` pracovní dny, `6` sobota, `+` neděle a svátky). U velké
části spojů ale vrací jiný den, než uvádí publikovaný jízdní řád téže linky.

Časy odjezdů, zastávky, čísla spojů ani realtime data problém nemají — jsou
v pořádku a shodují se se všemi ostatními zdroji.

## Nejkratší reprodukce

Linka 1, spoj 46, odjezd 06:36 ze zastávky Slovany,točna:

```
GET https://api.mhdonline.cz/pardubice/connections/1/46
→ "fixedCodes": ["+", "6", "@"]        tedy sobota + neděle a svátky
```

Zastávkový jízdní řád DPMP pro Slovany,točna, platnost od 1. 7. 2026
(`dpmp.cz/download/other/jr/platnost_20260701/JR100000013002B.htm`):

| | hodina 6 |
|---|---|
| Pracovní dny 1. 7. – 31. 8. | 06, 21, **36**, 51 |
| Soboty, neděle a svátky | 02, 32 |

Odjezd 06:36 je tedy **pracovní den**, nikoli víkend. Opačný případ je spoj 3
téže linky (04:33), který API označuje `["X", "@"]`, ale jízdní řád i CIS ho
řadí mezi víkendové.

## Rozsah

Porovnání proti CIS JDF (archiv `NeTEx_DrahyMestske.zip` ze 7. 8. 2026,
párováno přes číslo linky a číslo spoje):

| linka | shoda `fixedCodes` s CIS |
|------:|-------------------------:|
| 11 | 100,0 % |
| 1 | 79,1 % |
| 12 | 73,9 % |
| 4 | 69,5 % |
| 17 | 67,4 % |
| 13 | 64,5 % |
| 2 | 63,0 % |
| 5 | 54,5 % |
| 3 | 50,4 % |
| 7 | 50,0 % |
| 30 | 43,2 % |
| 27 | 40,0 % |

Celkem 1 024 z 1 592 spojů, tedy **64,3 % shody**.

## Co jsme vyloučili

Než jsme došli k tomuto závěru, vyloučili jsme postupně tyto možnosti:

1. **Špatný výklad kódů na naší straně.** Tabulku kódů jsme vzali doslova
   z bundlu vaší aplikace (`online.dpmp.cz/assets/index-*.js`): `X` = jede
   v pracovních dnech, `6` = jede v sobotu, `+` = jede v neděli a ve státem
   uznané svátky.

2. **Záměna velkých a malých písmen.** `/stops` vrací malé `x` (56 zastávek)
   a spoje velké `X`, obojí v téže odpovědi. Velikost písmen se zachovává.

3. **Špatné párování spojů.** Číslo spoje z API a `ServiceJourney/Name` v CIS
   se párují správně: u linky 1 se shoduje čas prvního odjezdu u 206 z 206
   spojů.

4. **Nová verze jízdního řádu, kterou CIS ještě nemá.** Linka 655001 má v CIS
   dvě verze a žádná budoucí. Verze platná od 1. 7. 2026 se shoduje s API
   v časech na 100 %, v kódech na 79 %.

5. **Nestabilita odpovědi.** Opakované dotazy vracejí tytéž hodnoty.

6. **Chybějící parametr dotazu.** `?date=`, `?day=` ani `?validity=` odpověď
   nemění; jiný endpoint s jízdním řádem jsme nenašli.

## Nezávislé potvrzení bez vnějšího zdroje

Odjezdy linky 1 ze Slovany,točna, seřazené podle kódu z API:

```
staré API   pracovní dny  05:04 05:20 05:36 05:51 06:06 06:21 06:36 06:51 …  takt 15 min
            víkend        06:02 06:32 07:02 07:32 08:00 08:30 09:00 …        takt 30 min

nové API    pracovní dny  06:06 06:21 ····· 06:51 … a mezi tím 09:00, 10:00, 11:30
            víkend        06:02 06:32 … a mezi tím 06:36, 07:21, 11:06, 13:51
```

Podle starých kódů vychází čistý patnáctiminutový a třicetiminutový takt.
Podle nových se oba takty prolínají — do pracovních dnů pronikají víkendové
časy `:00`/`:30` a do víkendu časy `:06`/`:21`/`:36`/`:51`.

## Poznámka

Vaše aplikace zobrazuje odjezdy pro aktuální okamžik a podle všeho podle dne
v týdnu nefiltruje — odznak `X` má v konfiguraci `disabled: true`. Vadná
hodnota se proto ve vašem produktu nemusí nijak projevit. Problém dopadá na
odběratele, kteří z API staví statický jízdní řád.

Rádi doplníme jakákoli data nebo ověříme opravu.
