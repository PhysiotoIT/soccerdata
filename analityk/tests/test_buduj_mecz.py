"""Testy generatora mecz.json (bez sieci: dane syntetyczne w formacie read_team_match_stats)."""

import itertools
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

KATALOG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KATALOG))

import buduj_mecz as bm

DRUZYNY = [
    "Lech Poznań",
    "Legia Warszawa",
    "Raków Częstochowa",
    "Jagiellonia Białystok",
    "Wisła Płock",
    "Pogoń Szczecin",
]
START = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)


def wiersze_meczu(gid, data, gosp, gosc, hg, ag, hxg, axg, hc=5, ac=4, hy=2, ay=2, hs=12, as_=10):
    """Dwa wiersze (gospodarz, gość) jak z Sofascore.read_team_match_stats."""
    wspolne = {"date": data, "game_id": gid, "league": "POL-Ekstraklasa", "season": "2627"}
    staty = {
        "xg": (hxg, axg),
        "corners": (hc, ac),
        "yellow_cards": (hy, ay),
        "red_cards": (0, 0),
        "shots": (hs, as_),
        "shots_on_target": (hs // 3, as_ // 3),
        "fouls": (12, 13),
        "offsides": (1, 2),
        "possession": (50, 50),
        "big_chances": (2, 1),
    }
    out = []
    for strona, team, opp, gf, ga, i, j in (
        ("home", gosp, gosc, hg, ag, 0, 1),
        ("away", gosc, gosp, ag, hg, 1, 0),
    ):
        r = dict(wspolne, team=team, venue=strona, opponent=opp, goals=gf, goals_against=ga)
        for kol, (h, a) in staty.items():
            vals = (h, a)
            r[kol], r[f"{kol}_against"] = vals[i], vals[j]
        r["game"] = f"{data:%Y-%m-%d} {gosp}-{gosc}"
        out.append(r)
    return out


def liga_rowna() -> pd.DataFrame:
    """Każdy z każdym dwa razy, wszystkie mecze 1:1 / xG 1.3–1.0 — wszyscy równi."""
    rows, gid = [], 0
    for k, (h, a) in enumerate(itertools.permutations(DRUZYNY, 2)):
        gid += 1
        rows += wiersze_meczu(gid, START + timedelta(days=3 * k), h, a, 1, 1, 1.3, 1.0)
    return pd.DataFrame(rows)


def test_dopasuj() -> None:
    assert bm.dopasuj("lech", DRUZYNY) == "Lech Poznań"
    assert bm.dopasuj("Rakow", DRUZYNY) == "Raków Częstochowa"
    assert bm.dopasuj("Legia W.", DRUZYNY) == "Legia Warszawa"
    assert bm.dopasuj("jagiellonia bialystok", DRUZYNY) == "Jagiellonia Białystok"
    with pytest.raises(ValueError, match="kilku"):
        bm.dopasuj("Wisła", [*DRUZYNY, "Wisła Kraków"])
    with pytest.raises(ValueError, match="Nie znam"):
        bm.dopasuj("Barcelona", DRUZYNY)


def test_dopasuj_cale_slowo_przed_poczatkiem() -> None:
    z_lechia = [*DRUZYNY, "Lechia Gdańsk"]
    assert bm.dopasuj("Lech", z_lechia) == "Lech Poznań"
    assert bm.dopasuj("Lechia", z_lechia) == "Lechia Gdańsk"
    assert bm.dopasuj("Lechi", z_lechia) == "Lechia Gdańsk"


def test_sezony() -> None:
    assert bm.sezon_biezacy(datetime(2026, 11, 4, tzinfo=timezone.utc)) == "26-27"
    assert bm.sezon_biezacy(datetime(2027, 3, 1, tzinfo=timezone.utc)) == "26-27"
    assert bm.sezon_biezacy(datetime(2027, 7, 20, tzinfo=timezone.utc)) == "27-28"
    assert bm.sezon_poprzedni("26-27") == "25-26"
    assert bm.sezon_poprzedni("00-01") == "99-00"
    for zapis in ("26-27", "2026-27", "2026/2027", "2627", "2026-2027"):
        assert bm.normalizuj_sezon(zapis) == "26-27"
    assert bm.sezon_poprzedni("2026-27") == "25-26"
    with pytest.raises(ValueError, match="sezonu"):
        bm.normalizuj_sezon("2026-28")


def test_wybierz_mecz_najblizszy_w_dowolnej_kolejnosci() -> None:
    teraz = datetime.now(tz=timezone.utc)
    harm = pd.DataFrame(
        [
            {"home_team": "Lech Poznań", "away_team": "Legia Warszawa", "home_score": None,
             "date": pd.Timestamp(teraz + timedelta(days=4)), "game_id": 1},
            {"home_team": "Legia Warszawa", "away_team": "Lech Poznań", "home_score": None,
             "date": pd.Timestamp(teraz + timedelta(days=150)), "game_id": 2},
            {"home_team": "Legia Warszawa", "away_team": "Lech Poznań", "home_score": 1.0,
             "date": pd.Timestamp(teraz - timedelta(days=60)), "game_id": 3},
        ]
    )  # fmt: skip
    mecz, zal = bm.wybierz_mecz(harm, "Legia Warszawa", "Lech Poznań")
    assert mecz["game_id"] == 1
    assert any("Zamieniono" in z for z in zal)
    mecz, zal = bm.wybierz_mecz(harm, "Lech Poznań", "Legia Warszawa")
    assert (mecz["game_id"], zal) == (1, [])
    with pytest.raises(ValueError, match="Brak nierozegranego"):
        bm.wybierz_mecz(harm, "Lech Poznań", "Raków Częstochowa")


def test_sedzia_bez_kartek_to_sedzia_nieznany() -> None:
    sedzia, uwaga = bm.sedzia_z_szczegolow(
        pd.Series({"referee": "Jan Kowalski", "referee_games": 50, "referee_yellow_cards": None})
    )
    assert sedzia["kartki_na_mecz"] is None and "brak statystyk" in uwaga
    df = bm.przygotuj(liga_rowna())
    stat, _ = bm.oblicz_statystyki(df, "Lech Poznań", "Legia Warszawa", sedzia)
    assert stat["Kartki"]["suma"] == pytest.approx(4.0)  # same drużyny: 2 + 2
    sedzia, uwaga = bm.sedzia_z_szczegolow(
        pd.Series({"referee": "Jan Kowalski", "referee_games": 50, "referee_yellow_cards": 240,
                   "referee_red_cards": float("nan")})
    )  # fmt: skip
    assert (sedzia["mecze"], sedzia["kartki_na_mecz"], uwaga) == (50, 4.8, None)
    assert bm.sedzia_z_szczegolow(pd.Series({"referee": None}))[0] is None


def test_poczatek_sezonu_srednie_z_zeszlego() -> None:
    prev = bm.przygotuj(liga_rowna())
    sr_prev = bm.srednie_ligi(prev)
    pusty = prev.iloc[0:0]
    with pytest.raises(ValueError, match="--poprzedni-sezon"):
        bm.srednie_ligi(pusty)
    lam = bm.oblicz_lambdy(pusty, "Lech Poznań", "Legia Warszawa", srednie_poprzednie=sr_prev)
    assert (lam["lh"], lam["la"]) == (pytest.approx(1.0), pytest.approx(1.0))
    assert lam["srednie_ligi"]["waga_sezonu"] == 0
    # 30 meczów bieżących po 3 gole gospodarzy -> pół na pół z zeszłym sezonem
    teraz = bm.przygotuj(liga_rowna()).assign(goals=3, goals_against=1)
    teraz = teraz[teraz["game_id"] <= 30]
    sr = bm.srednie_ligi(teraz, sr_prev)
    assert sr["gole_gosp"] == pytest.approx(2.0)


def test_rowna_liga_daje_srednie_ligi() -> None:
    df = bm.przygotuj(liga_rowna())
    lam = bm.oblicz_lambdy(df, "Lech Poznań", "Legia Warszawa")
    # xG gospodarzy (1.3) jest wyższe niż gole (1.0), ale tak samo w całej lidze:
    # współczynniki zostają 1.0, a λ = średnia goli ligi (bez skrzywienia xG vs gole)
    assert lam["srednie_ligi"]["gole_gosp"] == pytest.approx(1.0)
    assert lam["srednie_ligi"]["metryka_gosp"] == pytest.approx(0.7 * 1.3 + 0.3)
    assert all(v == pytest.approx(1.0, abs=1e-3) for v in lam["wspolczynniki"].values())
    assert (lam["lh"], lam["la"]) == (pytest.approx(1.0, abs=1e-3), pytest.approx(1.0, abs=1e-3))


def test_mocniejszy_atak_podnosi_lambde() -> None:
    df = liga_rowna()
    lech_dom = (df["team"] == "Lech Poznań") & (df["venue"] == "home")
    df.loc[lech_dom, ["goals", "xg"]] = [3, 2.8]
    rywale = (df["opponent"] == "Lech Poznań") & (df["venue"] == "away")
    df.loc[rywale, ["goals_against", "xg_against"]] = [3, 2.8]
    df = bm.przygotuj(df)
    mocny = bm.oblicz_lambdy(df, "Lech Poznań", "Legia Warszawa")
    zwykly = bm.oblicz_lambdy(df, "Raków Częstochowa", "Legia Warszawa")
    assert mocny["lh"] > zwykly["lh"] * 1.3


def test_sciaganie_bez_meczow_daje_prior() -> None:
    assert bm.wspolczynnik(float("nan"), 1.3, 0) == 1.0
    assert bm.wspolczynnik(2.6, 1.3, 0, prior=1.2) == 1.2
    assert bm.wspolczynnik(2.6, 1.3, 6) == pytest.approx(1.5)  # połowa drogi z 1.0 do 2.0


def test_statystyki_rozne_i_kartki() -> None:
    df = bm.przygotuj(liga_rowna())
    stat, zal = bm.oblicz_statystyki(df, "Lech Poznań", "Legia Warszawa")
    # stała liczba rożnych w każdym meczu -> wariancja 0 -> var_ratio obcięte do 1.0 (Poisson)
    assert stat["Rożne"] == {"gosp": 5.0, "gosc": 4.0, "var_ratio": 1.0}
    assert stat["Kartki"]["suma"] == pytest.approx(4.0)
    # sędzia z dużą próbą: 50/50, z małą: 30/70
    s1, _ = bm.oblicz_statystyki(
        df, "Lech Poznań", "Legia Warszawa", {"mecze": 100, "kartki_na_mecz": 6.0}
    )
    s2, _ = bm.oblicz_statystyki(
        df, "Lech Poznań", "Legia Warszawa", {"mecze": 5, "kartki_na_mecz": 6.0}
    )
    assert s1["Kartki"]["suma"] == pytest.approx(5.0)
    assert s2["Kartki"]["suma"] == pytest.approx(4.6)
    assert any("regulamin" in z for z in zal)


def test_var_ratio_z_danych() -> None:
    df = liga_rowna()
    dom = df["venue"] == "home"
    df.loc[dom & (df["game_id"] % 2 == 0), ["corners", "corners_against"]] = [9, 7]
    df.loc[dom & (df["game_id"] % 2 == 1), ["corners", "corners_against"]] = [2, 1]
    vr, z_danych = bm._var_ratio(df[dom]["corners"] + df[dom]["corners_against"], 1.15)
    assert z_danych
    assert vr == 2.0  # bardzo rozproszone dane -> obcięte do 2.0


def test_brak_xg_uzupelniany_golami() -> None:
    df = liga_rowna()
    df.loc[df["team"] == "Lech Poznań", "xg"] = float("nan")
    lam = bm.oblicz_lambdy(bm.przygotuj(df), "Lech Poznań", "Legia Warszawa")
    assert any("brak xG" in z for z in lam["zalozenia"])


def test_zbuduj_bez_przecieku_i_z_modelem(tmp_path: Path) -> None:
    df = liga_rowna()
    termin = START + timedelta(days=45)
    wynik = bm.zbuduj(
        df,
        "Lech Poznań",
        "Legia Warszawa",
        "POL-Ekstraklasa",
        data_meczu=termin,
        sedzia={"nazwisko": "Sędzia Testowy", "mecze": 50, "kartki_na_mecz": 4.4},
        absencje=[],
    )
    sr = wynik["kontekst"]["srednie_ligi"]
    assert sr["mecze"] == 15  # tylko mecze sprzed terminu: dni 0, 3, ..., 42
    assert wynik["kontekst"]["dni_odpoczynku"]["Lech Poznań"] is not None
    assert wynik["kontekst"]["absencje"] == ["Sofascore nie podaje jeszcze braków kadrowych."]
    assert wynik["mecz"].startswith("Lech Poznań – Legia Warszawa, POL-Ekstraklasa, 01.09")
    assert {"Rożne", "Strzały", "Strzały celne", "Faule", "Kartki"} <= set(wynik["statystyki"])

    wynik["rynki"] = ["Wynik 2:1", "1 & Over 1.5"]
    plik = tmp_path / "mecz.json"
    plik.write_text(json.dumps(wynik, ensure_ascii=False, default=str), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(KATALOG / "model_meczowy.py"), str(plik)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "λ model" in out
    assert "Rożne Over" in out
    assert "Kartki" in out
    assert "Wynik 2:1" in out and "1 & Over 1.5" in out  # dodatkowe rynki bez kursów
    assert "VALUE" not in out  # bez kursów nie ma sekcji value


def test_cli_z_csv(tmp_path: Path) -> None:
    csv = tmp_path / "sezon.csv"
    bm.przygotuj(liga_rowna()).to_csv(csv, index=False)
    wyj = tmp_path / "mecz.json"
    bm.main(
        [
            "lech",
            "legia",
            "--z-csv",
            str(csv),
            "--wyjscie",
            str(wyj),
            "--sedzia-mecze",
            "80",
            "--sedzia-kartki",
            "5.2",
        ]
    )
    wynik = json.loads(wyj.read_text(encoding="utf-8"))
    assert wynik["model"]["lh"] > 0 and wynik["model"]["la"] > 0
    assert wynik["statystyki"]["Kartki"]["suma"] == pytest.approx(0.5 * 5.2 + 0.5 * 4.0)
    assert wynik["wersja"] == "analityk-meczowy-v2"


def test_ciekawostki_i_forma() -> None:
    df = liga_rowna()
    # Lech wygrywa wysoko u siebie z Legią
    m = (
        (df["team"] == "Lech Poznań")
        & (df["opponent"] == "Legia Warszawa")
        & (df["venue"] == "home")
    )
    df.loc[m, ["goals", "goals_against"]] = [4, 0]
    df = bm.przygotuj(df)
    c = bm.ciekawostki(df, "Lech Poznań", "Legia Warszawa")
    assert any("Over 2.5" in x and "u siebie" in x for x in c)
    assert any(x.startswith("W tym sezonie: Lech Poznań u siebie 4:0") for x in c)
    f = bm.forma(df, "Lech Poznań")
    assert len(f) == 5 and all("xG" in x for x in f)


def test_kalibracja_bez_kursow(tmp_path: Path) -> None:
    import kalibracja

    log = tmp_path / "log.csv"
    log.write_text(
        "data,mecz,rynek,p_model,kurs_wziety,kurs_zamkniecia,stawka,podatek,wynik\n"
        "2026-11-01,A – B,Over 1.5,0.80,,,,,1\n"
        "2026-11-01,A – B,BTTS tak,0.60,,,,,0\n"
        "2026-11-02,C – D,1,0.50,2.10,2.00,10,0.88,1\n"
        "2026-11-02,C – D,Rożne Over 8.5,0.65,,,,,Z\n"
        "2026-11-03,E – F,Under 3.5,0.70,,,,,\n",
        encoding="utf-8",
    )
    import csv as _csv

    with log.open(encoding="utf-8") as fh:
        w = kalibracja.rozlicz(list(_csv.DictReader(fh)))
    assert w["n"] == 3  # zwrot i nierozliczony pominięte
    assert w["brier"] == pytest.approx((0.2**2 + 0.6**2 + 0.5**2) / 3)
    assert w["roi"] == pytest.approx(0.88 * 2.10 - 1)
    assert w["clv"] == pytest.approx(2.10 / 2.00 - 1)
    kalibracja.main([str(log)])


def test_kalibracja_polski_excel(tmp_path: Path) -> None:
    import kalibracja

    log = tmp_path / "log.csv"
    log.write_text(
        "data;mecz;rynek;p_model;kurs_wziety;kurs_zamkniecia;stawka;podatek;wynik\n"
        "2026-11-01;A – B;Over 1.5;82%;;;;;tak\n"
        "2026-11-01;A – B;BTTS tak;0,60;;;;;nie\n"
        "2026-11-02;C – D;1;55;;;;;W\n"
        "2026-11-02;C – D;Rożne Over 8.5;0.65;;;;;??\n",
        encoding="utf-8",
    )
    w = kalibracja.rozlicz(kalibracja.wczytaj(str(log)))
    assert (w["n"], w["nieczytelne"]) == (3, 1)
    assert w["brier"] == pytest.approx((0.18**2 + 0.6**2 + 0.45**2) / 3)


def test_csv_bez_game_id_i_ze_srednikami(tmp_path: Path) -> None:
    df = bm.przygotuj(liga_rowna()).drop(columns=["game_id", "game", "league", "season"])
    csv = tmp_path / "sezon.csv"
    df.to_csv(csv, index=False, sep=";", decimal=",")
    wyj = tmp_path / "mecz.json"
    bm.main(["Lech", "Legia", "--z-csv", str(csv), "--wyjscie", str(wyj)])
    wynik = json.loads(wyj.read_text(encoding="utf-8"))
    assert wynik["model"]["lh"] == pytest.approx(1.0, abs=1e-3)
    # tylko dwie drużyny w CSV -> ostrzeżenie o niepełnych średnich ligi
    dwie = df[df["team"].isin(["Lech Poznań", "Legia Warszawa"])]
    dwie[["date", "team", "opponent", "venue", "goals", "goals_against"]].to_csv(csv, index=False)
    bm.main(["Lech", "Legia", "--z-csv", str(csv), "--wyjscie", str(wyj)])
    wynik = json.loads(wyj.read_text(encoding="utf-8"))
    assert any("niepełnych" in z for z in wynik["zalozenia"])
    df.drop(columns=["venue"]).to_csv(csv, index=False)
    with pytest.raises(SystemExit, match="venue"):
        bm.main(["Lech", "Legia", "--z-csv", str(csv), "--wyjscie", str(wyj)])


def test_cli_sedzia_tylko_razem(tmp_path: Path) -> None:
    csv = tmp_path / "sezon.csv"
    bm.przygotuj(liga_rowna()).to_csv(csv, index=False)
    with pytest.raises(SystemExit):
        bm.main(["Lech", "Legia", "--z-csv", str(csv), "--sedzia-kartki", "5.2"])
    with pytest.raises(SystemExit, match="kilku"):
        bm.main(["L", "Raków", "--z-csv", str(csv), "--wyjscie", str(tmp_path / "m.json")])


def test_model_bez_powtorzen_rynkow(tmp_path: Path) -> None:
    cfg = {"model": {"lh": 1.5, "la": 1.1}, "rynki": ["Over 2.5", "over 2.5", "BTTS tak"],
           "kursy": {"BTTS tak": 1.8}}  # fmt: skip
    plik = tmp_path / "m.json"
    plik.write_text(json.dumps(cfg), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(KATALOG / "model_meczowy.py"), str(plik)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    tabela_p = out.stdout.split("=== VALUE")[0]  # tabela P; sekcja value osobno
    wiersze = [w.split()[0:2] for w in tabela_p.splitlines()]
    assert sum(w == ["Over", "2.5"] for w in wiersze) == 1
    assert sum(w == ["BTTS", "tak"] for w in wiersze) == 1
