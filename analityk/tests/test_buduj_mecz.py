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


def test_sezony() -> None:
    assert bm.sezon_biezacy(datetime(2026, 11, 4, tzinfo=timezone.utc)) == "26-27"
    assert bm.sezon_biezacy(datetime(2027, 3, 1, tzinfo=timezone.utc)) == "26-27"
    assert bm.sezon_biezacy(datetime(2027, 7, 20, tzinfo=timezone.utc)) == "27-28"
    assert bm.sezon_poprzedni("26-27") == "25-26"
    assert bm.sezon_poprzedni("00-01") == "99-00"


def test_rowna_liga_daje_srednie_ligi() -> None:
    df = bm.przygotuj(liga_rowna())
    lam = bm.oblicz_lambdy(df, "Lech Poznań", "Legia Warszawa")
    # gospodarze: 0.7 * 1.3 + 0.3 * 1 = 1.21 przy średniej 1 gola -> współczynnik ataku > 1
    assert lam["srednie_ligi"]["gole_gosp"] == pytest.approx(1.0)
    w = lam["wspolczynniki"]
    assert w["atak_gosp"] == pytest.approx(1 + 0.21 * 5 / 11, abs=1e-3)
    assert w["obrona_gosp"] == pytest.approx(1.0, abs=1e-3)  # 0.7*1.0 + 0.3*1 = 1.0
    assert lam["lh"] == pytest.approx(w["atak_gosp"] * w["obrona_gosc"], abs=1e-3)


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
