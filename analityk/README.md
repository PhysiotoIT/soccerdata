# Analityk meczowy v2 — dane z Sofascore

Narzędzia do skilla `analityk-meczowy-v2`: pobierają statystyki Ekstraklasy (i innych lig
z `LEAGUE_DICT`) przez ten fork soccerdata i budują `mecz.json` dla `model_meczowy.py`.

## Instalacja

```bash
git clone --depth 1 https://github.com/PhysiotoIT/soccerdata
pip install -e soccerdata          # w sandboxie Claude: pip install --break-system-packages -e soccerdata
```

Dane idą z `api.sofascore.com`. W sandboxie Claude ta domena musi być na liście dozwolonych
(Ustawienia > Capabilities); na własnym komputerze działa od razu. Pobrane mecze są
zapisywane w `~/soccerdata/data/Sofascore`, więc kolejne analizy pobierają tylko nowe mecze.

## Użycie

```bash
python3 analityk/buduj_mecz.py --lista "Lech"                    # najbliższe mecze drużyny
python3 analityk/buduj_mecz.py "Lech" "Legia" --wyjscie mecz.json
python3 analityk/buduj_mecz.py "Lech" "Legia" --poprzedni-sezon   # mała próba, start sezonu
python3 analityk/model_meczowy.py mecz.json
```

Nazwy drużyn wystarczą skrócone, bez polskich znaków. Jeśli kolejność jest odwrotna niż
w terminarzu, skrypt sam ustawi gospodarza i napisze to w założeniach.

`mecz.json` zawiera:

- `model` — λ gospodarza i gościa (wzór ze skilla: xG/gole 70/30, sezon/forma 60/40,
  dom/wyjazd osobno, ściąganie do średniej wagą n/(n+6));
- `statystyki` — rożne, strzały, strzały celne, faule (gosp/gosc) i kartki (suma, z sędzią);
  `var_ratio` liczone z meczów ligi, gdy jest ich co najmniej 30;
- `kontekst` — forma (5 meczów z xG), ciekawostki, dni odpoczynku, sędzia, absencje;
- `zalozenia`, `zrodla`, `korekty` — wszystko, co trzeba pokazać w raporcie.

## Bez dostępu do Sofascore

`--eksport sezon.csv` zapisuje statystyki sezonu do CSV; `--z-csv sezon.csv` liczy z pliku
(opcjonalnie `--data 2026-11-08T17:30 --sedzia-mecze 120 --sedzia-kartki 4.9`).

## Co dodaje fork (w `soccerdata/sofascore.py`)

- `POL-Ekstraklasa` (ID turnieju 202) — ligi spoza domyślnej listy Sofascore można podać ID;
- `read_team_match_stats()` — xG, rożne, kartki, strzały, faule, spalone, posiadanie,
  wielkie szanse na drużynę i mecz, z kolumnami `_against` dla rywala;
- `read_event_details()` — stadion i sędzia z liczbą meczów i kartek w karierze;
- `read_missing_players()` — kontuzjowani, zawieszeni i niepewni z zapowiedzi składów.

Sofascore nie ma oficjalnego publicznego API: struktura odpowiedzi może się zmienić,
a nadmiar zapytań kończy się blokadą. Do własnego użytku.

## Testy

```bash
pytest analityk/tests tests/test_Sofascore_offline.py
```
