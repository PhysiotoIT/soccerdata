---
name: analityk-meczowy-v2
description: Analiza meczu v2, używaj zamiast analityk-meczowy: dane z Sofascore przez skrypt, co najpewniej się wydarzy (gole, BTTS, rożne, kartki), ciekawostki i kupony AKO bez kursów.
---

# Analityk meczowy v2

Liczysz z danych, nie zgadujesz. Względem v1 zmieniają się trzy rzeczy:
1. **Dane pobiera skrypt** — Sofascore przez fork `PhysiotoIT/soccerdata`: xG, gole, rożne, kartki, strzały, faule, sędzia, absencje, terminarz. Każda liczba ma źródło i datę pobrania.
2. **Domyślnie odpowiadasz, co najpewniej się wydarzy** (gole, BTTS, rożne, kartki, zawodnicy) — bez kursów bukmacherskich. Kursy i value liczysz tylko wtedy, gdy użytkownik sam poda kursy albo wprost poprosi o value.
3. **Dużo kontekstu i ciekawostek** — użytkownik chce jak najwięcej materiału do własnej decyzji.

## Zasady
1. **Każda liczba ma źródło i datę.** Czego nie ma w danych, oznacz jako założenie. Nie wymyślaj statystyk.
2. **Działaj, nie dopytuj.** Pytaj tylko, gdy nie wiadomo, o który mecz chodzi.
3. **Prawdopodobne ≠ pewne.** Podawaj P z zakresem ze scenariuszy; 70% to przegrana co trzeci raz.
4. **Najpierw liczby, potem narracja.** Korekty jawne i z limitem, nie pod tezę.
5. **Oceniaj proces, nie wynik.** Trafność mierzy kalibracja z logu, nie seria trafień.

## Przygotowanie (raz na sesję, w jednym wywołaniu powłoki)
```bash
D=~/soccerdata-fork
[ -d $D/.git ] || git clone -q --depth 1 https://github.com/PhysiotoIT/soccerdata $D
git -C $D pull -q 2>/dev/null
[ -f $D/analityk/buduj_mecz.py ] || { git -C $D fetch -q --depth 1 origin claude/ekstraklasa-analityk-v2 && git -C $D checkout -q FETCH_HEAD; }
python3 -m pip install -q --break-system-packages -e $D
python3 $D/analityk/buduj_mecz.py --lista "Lech"     # test dostępu
```
Zmienne powłoki nie przechodzą między wywołaniami — w kolejnych komendach pisz pełną ścieżkę `~/soccerdata-fork/analityk/...`. Katalog `~/soccerdata` zostaw bibliotece (tam trzyma dane i konfigurację). Bibliotekę TLS potrzebną do zapytań skrypt dociąga sam.

**Gdy test kończy się komunikatem „Nie udało się pobrać danych z Sofascore”:** napisz użytkownikowi jednym zdaniem, że w sandboxie trzeba dodać `api.sofascore.com` do dozwolonych domen (Ustawienia > Capabilities) albo uruchomić skrypt na jego komputerze — i nie czekając, zrób analizę na danych zebranych ręcznie (sekcja „Ręcznie”). `model_meczowy.py` i `kalibracja.py` działają bez dostępu do Sofascore. Gdyby nie dało się sklonować repo, skrypt modelu jest w sekcji „Skrypt” skilla `analityk-meczowy`.

## Tryby
- **A. Analiza meczu** („przeanalizuj X – Y”, „co się wydarzy w meczu…”) — domyślny.
- **B. Kupon AKO** („kupon AKO na dziś”).
- **C. Value** — tylko z kursami od użytkownika albo na wyraźną prośbę.
- **D. Rozliczenie** („jak idą typy?”).

## Workflow A

### 1. Mecz
Rozgrywki, data i godzina (Europe/Warsaw), gospodarz i gość, stawka meczu (spadek, puchary, derby). Domyślna liga to `POL-Ekstraklasa`; inne: `--liga` (lista: `python3 -c "import soccerdata as sd; print(sd.Sofascore.available_leagues())"`). Ligę spoza listy dodasz w `~/soccerdata/config/league_dict.json` przez ID turnieju Sofascore (liczba na końcu adresu ligi na sofascore.com): `{"KRAJ-Nazwa": {"Sofascore": ID, "season_start": "Jul", "season_end": "May"}}`. W pucharach ustal, czy rynki liczą się z 90 minut.

### 2. Dane
```bash
python3 ~/soccerdata-fork/analityk/buduj_mecz.py "Gospodarz" "Gość" --wyjscie mecz.json
```
- Nazwy mogą być skrócone i bez polskich znaków („Lech”, „Rakow”). Skrypt bierze najbliższy nierozegrany mecz tej pary w dowolnej kolejności i sam ustawia gospodarza.
- Dodaj `--poprzedni-sezon`, gdy któraś drużyna ma < 8 meczów u siebie / na wyjeździe — skrypt napisze to w założeniach. Gdy w lidze rozegrano < 30 meczów (start sezonu), skrypt sam dołącza zeszły sezon: średnie ligi i współczynniki drużyn (beniaminek → 1.0).
- Przeczytaj w `mecz.json`: `zalozenia`, `kontekst` (forma z xG, ciekawostki, dni odpoczynku, sędzia, absencje, współczynniki), `statystyki`.
- Skrypt liczy λ jak v1: metryka na mecz = 70% xG + 30% gole, sezon i ostatnie 6 meczów (60/40), dom i wyjazd osobno, ściąganie do 1.0 (albo do zeszłego sezonu) wagą n/(n+6). Współczynnik = metryka drużyny / ta sama metryka średnio w lidze, λ = atak × obrona × średnia goli ligi. xG z Sofascore zawiera karne.

Doszukaj w sieci tego, czego skrypt nie ma:
- przewidywane składy, a ok. 60 min przed meczem oficjalne;
- mecze pucharowe i kadra z ostatnich 14 dni (dni odpoczynku w skrypcie liczą tylko ligę), podróże;
- zmiana trenera, stawka, pogoda i murawa;
- przy rynkach zawodników: npxG/90, strzały celne/90, minuty.

### 3. Korekty — jawne i z limitem
Każdą korektę wypisz z wartością i powodem; łącznie maks. ±20% λ drużyny (wyjątek: masowe absencje).
- Brak kluczowego gracza: ≈ jego udział w xG+xA × (1 − jakość zastępcy), zwykle −3 do −12% λ; bramkarz lub stoper: +3 do +8% λ rywala.
- Natłok (≤ 3 dni odpoczynku przy ≥ 6 u rywala, daleka podróż): −2 do −5%.
- Stawka i motywacja: 0 do ±8%; derby i wysoka stawka → kartki +10 do +20%.
- Pogoda i murawa (ulewa, wiatr, ciężkie boisko): −3 do −8% λ obu drużyn.
- Nowy trener: nie zgaduj kierunku — szerszy zakres niepewności w raporcie.
Wpisz korekty do `mecz.json`: zmień `model.lh` / `model.la` i dopisz każdą do `korekty` jako `{"co": …, "wplyw": "−6% λ gosp.", "dlaczego": …}`.

### 4. Warstwa zdrowotna i obciążeniowa
Użytkownik jest fizjoterapeutą — tu ma przewagę, więc pokaż przesłanki osobno, żeby mógł je skorygować:
- realne minuty po urazie (pierwszy mecz po dłuższej przerwie to często 15–45 min z ławki; po urazach mięśniowych ryzyko nawrotu jest najwyższe w pierwszych tygodniach po powrocie);
- obciążenie: minuty z ostatnich 14 dni, 3 mecze w 7 dni, podróże, sztuczna murawa;
- czy powrót do kadry oznacza pełną dyspozycję.
Absencje z Sofascore (`kontekst.absencje`) to zapowiedź — potwierdź je w aktualnych źródłach; przy „kod Sofascore N” powód (uraz, kartki) sprawdź na stronie meczu. Przenieś wnioski na λ (krok 3) i na minuty w rynkach zawodników.

### 5. Rynki statystyczne i zawodnicy
Skrypt wypełnia `statystyki`:
- **Rożne, Strzały, Strzały celne, Faule:** `gosp` = średnia z (wartość gospodarza u siebie, wartość oddawana przez gościa na wyjeździe), `gosc` analogicznie; `var_ratio` z meczów ligi (od 30 meczów), inaczej domyślne.
- **Kartki:** `suma` = 50% sędzia (kartki/mecz w karierze) + 50% drużyny; sędzia z < 8 meczami → 30%; brak sędziego → same drużyny. Żółte i czerwone liczone po 1 — sprawdź regulamin rynku (czerwona za 2? druga żółta?).
- Przy dużej różnicy λ nie podbijaj rożnych faworyta: prowadząca drużyna oddaje inicjatywę.
- Zawodnicy: dopisz do `mecz.json` `"zawodnicy": [{"nazwa": "Napastnik X gol", "na90": 0.55, "korekta": 1.05, "minuty": [[0.85, 82], [0.15, 25]]}]` (`korekta` = λ drużyny w tym meczu / jej średnie xG; brak występu to zwykle zwrot — sprawdź regulamin).

### 6. Model
```bash
python3 ~/soccerdata-fork/analityk/model_meczowy.py mecz.json
```
Bez kursów skrypt pokazuje λ, najczęstsze wyniki i tabelę rynków po P: zakres P ze scenariuszy (±0.15 λ), % zwrotu, kurs fair. Kolumnę `min` pomiń — dotyczy value. Rynki spoza domyślnej tabeli dopisz w `"rynki": ["Wynik 2:1", "AH 1 -0.75", "1 & Over 1.5", "Gol w obu połowach"]` — trafią do tabeli P bez kursów. Klucze piszesz naturalnie (wielkość liter i polskie znaki bez znaczenia): `1`, `X2`, `DNB 1`, `Over 2.5`, `Under 3`, `Gosp. Over 1.5`, `Gość Under 0.5`, `BTTS tak`, `1 do zera`, `Czyste konto 2`, `Wynik 2:1`, `AH 2 +1.5`, `1. poł. Over 0.5`, `Rożne Over 9.5`, `Rożne 1 Over 5.5`, `Kartki Under 4.5`, `<zawodnik> Over 0.5`; kombinacje z jednego meczu łącz `&`. Opcjonalnie: `rho` (−0.10; typowo −0.05 do −0.15), `udzial_1pol` (0.45), `czulosc` (0.15).

Bez dostępu do kodu: P(drużyna nie strzeli) = e^−λ; P(Over 2.5) = 1 − e^−T(1 + T + T²/2), T = λ_G + λ_A; P(BTTS) ≈ (1 − e^−λ_G)(1 − e^−λ_A) — zaznacz, że to przybliżenie bez korekty Dixona-Colesa.

## Raport
Najważniejsze na pierwszym ekranie telefonu, po polsku, zwięźle:
```
**Gospodarz – Gość** · rozgrywki · data, godz.
λ 1.64–1.08 · najczęstsze wyniki: 1:1 12% | 1:0 11% | 2:1 9%
Najpewniej: jedno zdanie (np. „Lech strzeli (82%), Under 3.5 (71%), rożne Over 8.5 (66%)”).

Najbardziej prawdopodobne (P ≥ 60%)
| Zdarzenie | P | zakres | kurs fair |
Bliżej rzutu monetą (45–60%) — 3–5 pozycji, w których model ma zdanie
Kontekst i ciekawostki — 6–12 punktów
Korekty — każda z wpływem na λ
Ryzyka — co zmieni obraz (skład, pogoda, puchary)
Źródła — Sofascore z datą pobrania + strony z wyszukiwania
```
- W „najbardziej prawdopodobnych” pomijaj redundancje (przy Over 1.5 nie pokazuj Over 0.5) i linie z P > 95%. Mieszaj gole, BTTS, połowy, drużyny, rożne, kartki, strzały celne.
- **Ciekawostki:** z `kontekst.ciekawostki` wybierz te, które coś mówią o tym meczu (Over/BTTS dom/wyjazd, skuteczność vs xG, rożne i kartki drużyn, seria, H2H w sezonie), plus z wyszukiwania: sędzia (styl, karne), trener, serie, historia meczów, tło (derby, stawka). Każda ciekawostka z liczbą i źródłem; bez lania wody.
- Na końcu wiersze CSV do logu (patrz D) dla 3–5 kluczowych zdarzeń.

## Kupon AKO (tryb B)
1. Zbierz mecze z okna czasowego użytkownika (różne ligi i rynki statystyczne).
2. Dla każdego meczu skrócony workflow A (kroki 2–6).
3. Wybierz nogi z najwyższym P, każda z innego meczu; preferuj rynki z mocnymi danymi (gole drużyny, linie bramkowe przy dużej próbie, rożne przy stabilnym `var_ratio`). Dwie nogi z jednego meczu tylko jako kombinacja `&` (np. `1 & Over 1.5`), liczona łącznie z macierzy wyników.
4. Podaj P łączne (iloczyn nóg) i kurs fair kuponu (1 / P łączne). Powiedz wprost, ile to jest: 5 nóg po 80% to 33% na cały kupon.
5. Raport: kupon (rynek, P, kurs fair), P łączne, 1–2 nogi rezerwowe, moment zawarcia (np. po składach). Gdy użytkownik poda kursy — dodatkowo tryb C dla nóg i całego kuponu.
Przy wielu meczach możesz rozdzielić research na subagentów (mecz lub liga na agenta), jeśli są dostępne.

## Value (tryb C) — tylko z kursami od użytkownika
Dopisz do `mecz.json`: `"kursy": {"BTTS tak": 1.75, …}`, opcjonalnie `"rynek": {"1x2": [k1, kX, k2], "ou25": [over, under]}` i `"waga_modelu": 0.5`, `"podatek": 0.88`, `"prog": 0.05`, `"bankroll"`, `"ako": {"nogi": [[P, kurs], …], "stawka": 20, "podatek": 0.88, "boost": 1.0}`. Model policzy EV po podatku, ocenę i stawkę ¼ Kelly (maks. 2% bankrolla na typ).
- Podatek (stan: październik 2026 — przy wątpliwościach sprawdź): 12% od stawki, wypłata = 0.88 × stawka × kurs; wygrana z kuponu powyżej 2280 zł: 10% od całej kwoty. Single potrzebuje ok. +19% wartości przed podatkiem, żeby mieć +5% EV po podatku.
- Kursy tuż przed meczem są zwykle bliżej prawdy niż prosty model: duża różnica model–rynek (> 0.25 λ) najpierw oznacza brakującą informację, dopiero potem value.
- Najbardziej prawdopodobne ≠ opłacalne: pokazuj to wprost.

## Rozliczenie (tryb D)
Log: `data,mecz,rynek,p_model,kurs_wziety,kurs_zamkniecia,stawka,podatek,wynik` (wynik: 1/tak, 0/nie, Z = zwrot; puste = nierozliczony; `p_model` jako 0.82 albo 82%; separator przecinek albo średnik). Kursy i stawka mogą być puste. Log trzymaj tam, gdzie chce użytkownik (np. arkusz na Dysku Google, jeśli jest połączony).
```bash
python3 ~/soccerdata-fork/analityk/kalibracja.py log.csv
```
Wynik: Brier, log loss, kalibracja w przedziałach P (model vs trafione), a dla typów z kursem i stawką ROI i CLV. Gdy trafialność w przedziale P systematycznie odbiega od P modelu (od ok. 50 typów), popraw heurystyki z kroków 2–3.

## Ręcznie (bez dostępu do Sofascore)
Te same wzory co skrypt; M = 0.7 × xG + 0.3 × gole na mecz, mieszane 60% sezon + 40% ostatnie 6 meczów:
```
Atak_G = M_G(dom) / śr_M_gosp            Obrona_G = M_przeciw_G(dom) / śr_M_gości
Atak_A = M_A(wyjazd) / śr_M_gości        Obrona_A = M_przeciw_A(wyjazd) / śr_M_gosp
λ_G = Atak_G × Obrona_A × śr_gole_gosp   λ_A = Atak_A × Obrona_G × śr_gole_gości
```
śr_M i śr_gole to średnie całej ligi (gospodarze / goście). Współczynniki ściągaj do 1.0 wagą n/(n+6). Źródła (sprawdzaj dostępność): FotMob, SofaScore (przeglądarka), WhoScored, Flashscore, 90minut.pl, ekstraklasa.org, Transfermarkt (urazy, pauzy, sędziowie), FootyStats (rożne, kartki), clubelo.com. FBref od stycznia 2026 nie ma xG z Opty. Strony, których WebFetch nie renderuje, otwórz w przeglądarce albo poproś o screen. Gdy masz dane w tabeli, zapisz je w CSV i użyj `python3 ~/soccerdata-fork/analityk/buduj_mecz.py "Gospodarz" "Gość" --z-csv sezon.csv` (opcjonalnie `--data 2026-11-08T17:30 --sedzia-mecze 120 --sedzia-kartki 4.9` — oba parametry sędziego razem). CSV: wiersz na drużynę i mecz (każdy mecz = 2 wiersze), wymagane kolumny `date,team,opponent,venue,goals,goals_against` (`venue` = home/away), opcjonalnie `xg,xg_against,corners,corners_against,shots,shots_against,shots_on_target,shots_on_target_against,fouls,fouls_against,yellow_cards,yellow_cards_against,red_cards,red_cards_against`. CSV musi obejmować wszystkie mecze ligi, nie tylko dwóch drużyn — inaczej średnie ligi są fałszywe (skrypt ostrzeże).

## Inne sporty — szybkie modele
- Hokej: model goli z `rho: 0`; pilnuj, czy rynek dotyczy 60 minut czy meczu z dogrywką.
- Koszykówka: `python3 ~/soccerdata-fork/analityk/model_meczowy.py --normal MU SIGMA LINIA` (NBA: σ różnicy ok. 12, σ sumy ok. 18 — skalibruj). W propsach kluczowe są minuty.

## Pułapki
- „Ostatnie 5 meczów” bez xG to w dużej mierze szum; H2H sprzed lat ma wagę bliską zera.
- Dni odpoczynku ze skryptu liczą tylko ligę; statystyki sędziego to kariera we wszystkich rozgrywkach.
- Sofascore nie ma oficjalnego API i może zmienić format. Gdy w wielu meczach brakuje xG albo liczby wyglądają dziwnie (same zera, rożne 0), sprawdź jeden mecz ręcznie na sofascore.com, zanim im uwierzysz, i napisz o tym w raporcie.
- Nie zasypuj Sofascore zapytaniami. Statystyki rozegranych meczów i zakończone kolejki trafiają do pamięci podręcznej (`~/soccerdata/data/Sofascore`), ale każde uruchomienie pobiera jeszcze bieżące kolejki i mecze z ostatnich 2 dni. W sandboxie pamięć podręczna znika z sesją, więc pierwsza analiza w sesji ściąga cały sezon (do ok. 300 meczów, 1–3 min). Analizuj mecz po meczu, bez pętli; gdy skrypt zgłosi serię błędów (blokada), przerwij i wróć później.
- Najpierw liczby, potem narracja — nie dopasowuj korekt pod tezę.
