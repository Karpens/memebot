# Meme Bot

Nočný obchodník s čerstvými meme coinmi na Solane, zatiaľ **len na papieri**:
bez peňaženky a bez skutočných peňazí. Vyzerá ako bot z videa „just 1 prompt“:
jeden prompt (`prompt.txt`), šesť agentov, štart s 70 $, od 23:00 do 07:00.

## Čo robí

1. **SCANNER** každú minútu číta nové páry z GeckoTerminalu a DexScreeneru.
   Berie len páry mladšie ako 6 hodín na pump.fun, PumpSwap, Raydiume alebo
   Meteore s likviditou aspoň 40 000 $ a objemom aspoň 25 000 $ za 5 minút.
   Označí coin, keď nákupy tvoria aspoň 70 % a objem je aspoň 5-krát nad
   hodinovým priemerom alebo za 10 minút pribudne aspoň 15 % držiteľov.
   Sám nikdy neobchoduje.
2. **RUG CHECK** číta správu RugChecku. Zamietne coin, keď:
   - mint alebo freeze authority je otvorená,
   - dev peňaženka má viac ako 5 %,
   - top 10 držiteľov (bez poolu) má viac ako 35 %,
   - sú tam insideri alebo bundle,
   - LP nie je spálené ani zamknuté,
   - má menej ako 300 držiteľov.
   Potom nákup na skúšku hneď „predá“ cez Jupiter. Keď sa predať nedá alebo by
   stratil viac ako 10 %, je to honeypot.
3. **SIZER** dá do obchodu 25 % zostatku, najmenej 5 $.
4. **RISK**:
   - najviac 3 otvorené pozície a 12 obchodov za noc,
   - po 3 stratách po sebe 30 minút pauza,
   - posledných 30 minút bez nových vstupov,
   - pri −35 % za noc koniec.
5. **EXIT**:
   - pri +50 % predá polovicu, zvyšok drží trailing stop 20 % pod vrcholom,
   - stop loss −25 %,
   - po 45 minútach bez +10 % predá,
   - do 06:55 predá všetko.
6. **JOURNAL** zapíše každé rozhodnutie aj s dôvodom a ráno správu.

Každý nákup a predaj má cenu zo skutočnej ponuky Jupitera pre tú sumu (s
poplatkami poolu a dopadom na cenu), mínus sieťový poplatok a 1 % za
oneskorenie. Skutočný obchod môže dopadnúť horšie: zlyhaná transakcia,
sendvič, cena, ktorá sa pohne za tie sekundy.

Sledovanie „smart money“ a KOL peňaženiek z videa tu nie je: potrebuje
platený Solana RPC a zoznam peňaženiek.

## Spustenie

```
python3 memebot/bot.py run --data DIR                       # noc 23:00 až 07:00
python3 memebot/bot.py run --data DIR --minutes 15 --anytime   # skúška kedykoľvek
python3 memebot/bot.py page --data DIR --out bot.html       # stránka s výsledkami
```

Na GitHube je workflow **Meme Bot**. Každú noc o 23:02 ho spustia Claude
hodiny a beží do 07:00 (dva joby, lebo job smie bežať najviac 6 hodín).
Repozitár je verejný, preto sú minúty GitHub Actions zadarmo. Tlačidlo Run
workflow ho spustí hneď (s „anytime“ aj cez deň). Vypneš ho premennou
`MEMEBOT_OFF=true`. Dáta ukladá do vetvy `meme-data`.

Nie je to finančné poradenstvo. Väčšina nových meme coinov stratí väčšinu
hodnoty za pár hodín.
