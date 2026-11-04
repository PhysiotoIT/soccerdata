# Analityk meczowy v2 — dane z Sofascore

Narzędzia do skilla `analityk-meczowy-v2`: pobierają statystyki Ekstraklasy (i innych lig
z `LEAGUE_DICT`) przez ten fork soccerdata i budują `mecz.json` dla `model_meczowy.py`.

## Instalacja

```bash
git clone --depth 1 https://github.com/PhysiotoIT/soccerdata soccerdata-fork
pip install -e soccerdata-fork     # w sandboxie Claude: pip install --break-system-packages -e soccerdata-fork
```

Katalog `soccerdata-fork`, a nie `soccerdata`: `~/soccerdata` to domyślne miejsce na dane
i konfigurację biblioteki.

Dane idą z `api.sofascore.com`. W sandboxie Claude ta domena musi być na liście dozwolonych
(Ustawienia > Capabilities); na własnym komputerze działa od razu. Statystyki rozegranych
meczów i zakończone kolejki są zapisywane w `~/soccerdata/data/Sofascore`; kolejne uruchomienia
pobierają już tylko bieżące kolejki i mecze z ostatnich 2 dni. Między zapytaniami jest krótka
przerwa, a po 3 nieudanych pobraniach z rzędu (blokada) skrypt przestaje pytać Sofascore.

## Użycie

```bash
python3 analityk/buduj_mecz.py --lista "Lech"                    # najbliższe mecze drużyny
python3 analityk/buduj_mecz.py "Lech" "Legia" --wyjscie mecz.json
python3 analityk/buduj_mecz.py "Lech" "Legia" --poprzedni-sezon   # mała próba, start sezonu
python3 analityk/model_meczowy.py mecz.json
python3 analityk/kalibracja.py log.csv                          # trafność typów, także bez kursów
```

Nazwy drużyn wystarczą skrócone, bez polskich znaków. Skrypt bierze najbliższy nierozegrany
mecz tej pary w dowolnej kolejności, sam ustawia gospodarza i pisze to w założeniach. Gdy
w lidze rozegrano mniej niż 30 meczów, sam dołącza zeszły sezon (średnie ligi i priory).

`mecz.json` zawiera:

- `model` — λ gospodarza i gościa (wzór ze skilla: xG/gole 70/30, sezon/forma 60/40,
  dom/wyjazd osobno, ściąganie do średniej wagą n/(n+6); współczynnik = metryka drużyny /
  ta sama metryka w lidze, więc różnica xG ligi względem goli nie przesuwa λ);
- `statystyki` — rożne, strzały, strzały celne, faule (gosp/gosc) i kartki (suma, z sędzią);
  `var_ratio` liczone z meczów ligi, gdy jest ich co najmniej 30;
- `kontekst` — forma (5 meczów z xG), ciekawostki, dni odpoczynku, sędzia, absencje;
- `zalozenia`, `zrodla`, `korekty` — wszystko, co trzeba pokazać w raporcie.

## Bez dostępu do Sofascore

`--eksport sezon.csv` zapisuje statystyki sezonu do CSV; `--z-csv sezon.csv` liczy z pliku
(opcjonalnie `--data 2026-11-08T17:30 --sedzia-mecze 120 --sedzia-kartki 4.9`).

CSV z innego źródła: wiersz na drużynę i mecz, wymagane kolumny
`date,team,opponent,venue,goals,goals_against` (`venue` = `home`/`away`), opcjonalnie `xg`,
`corners`, `shots`, `shots_on_target`, `fouls`, `yellow_cards`, `red_cards` i ich wersje
`*_against`. Separator przecinek albo średnik. Plik musi obejmować wszystkie mecze ligi —
z samych meczów dwóch drużyn średnie ligi wychodzą fałszywe.

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
