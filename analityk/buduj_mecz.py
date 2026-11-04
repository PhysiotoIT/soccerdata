#!/usr/bin/env python3
"""Buduje mecz.json dla model_meczowy.py z danych Sofascore (fork PhysiotoIT/soccerdata).

  python3 analityk/buduj_mecz.py "Lech" "Legia"                 najbliższy mecz Lech (dom) – Legia
  python3 analityk/buduj_mecz.py "Lech" "Legia" --wyjscie mecz.json --poprzedni-sezon
  python3 analityk/buduj_mecz.py --lista "Lech"                  najbliższe mecze drużyny
  python3 analityk/buduj_mecz.py "Lech" "Legia" --eksport sezon.csv   zapisz też statystyki sezonu
  python3 analityk/buduj_mecz.py "Lech" "Legia" --z-csv sezon.csv     licz z CSV, bez pobierania

λ liczone jak w skillu: współczynniki ataku/obrony z xG i goli (70/30), sezon i forma (60/40),
dom/wyjazd osobno, ściąganie do średniej wagą n/(n+6). Rynki statystyczne: rożne, strzały,
strzały celne, faule (gosp/gosc) i kartki (suma z drużyn i sędziego).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

WARSZAWA = ZoneInfo("Europe/Warsaw")
WAGA_XG = 0.7  # reszta: gole
WAGA_SEZONU = 0.6  # reszta: ostatnie mecze (forma)
K_SCIAGANIA = 6  # współczynnik ściągany do średniej wagą n / (n + K)
STATY_STRON = {  # nazwa rynku w modelu -> kolumna danych
    "Rożne": "corners",
    "Strzały": "shots",
    "Strzały celne": "shots_on_target",
    "Faule": "fouls",
}
VAR_RATIO_DOMYSLNE = {"corners": 1.15, "shots": 1.2, "shots_on_target": 1.2, "fouls": 1.15}
VAR_RATIO_KARTKI = 1.25
MIN_MECZOW_VAR = 30
BRAK_DOSTEPU = """Nie udało się pobrać danych z Sofascore ({blad}).
Możliwe przyczyny i co zrobić:
- sandbox Claude: dodaj api.sofascore.com do dozwolonych domen (Ustawienia > Capabilities)
  albo uruchom skrypt na swoim komputerze;
- Sofascore blokuje adres IP (403): spróbuj później albo z innej sieci;
- dane z innego miejsca: zapisz je w CSV i użyj --z-csv (kolumny jak w --eksport)."""


# ---------- dopasowanie nazw ----------


def _tokeny(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", str(s).translate(str.maketrans("łŁ", "lL")))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()


def dopasuj(nazwa: str, druzyny: list[str]) -> str:
    """Zwróć drużynę z listy pasującą do wpisanej nazwy ("lech", "Rakow", "Legia W.")."""
    q = _tokeny(nazwa)
    if not q:
        raise ValueError("Pusta nazwa drużyny.")
    dokladne = [d for d in druzyny if _tokeny(d) == q]
    if len(dokladne) == 1:
        return dokladne[0]
    kandydaci = [
        d for d in druzyny if all(any(t.startswith(x.rstrip(".")) for t in _tokeny(d)) for x in q)
    ]
    if len(kandydaci) == 1:
        return kandydaci[0]
    if not kandydaci:
        raise ValueError(
            f"Nie znam drużyny „{nazwa}”. Drużyny w danych: {', '.join(sorted(druzyny))}"
        )
    raise ValueError(f"„{nazwa}” pasuje do kilku drużyn: {', '.join(sorted(kandydaci))}")


# ---------- dane ----------


def przygotuj(df: pd.DataFrame) -> pd.DataFrame:
    """Spłaszcz wynik Sofascore.read_team_match_stats (albo CSV z --eksport) i posortuj po dacie."""
    df = df.reset_index() if "team" not in df.columns else df.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    if "red_cards" in df.columns and "yellow_cards" in df.columns:
        df["cards"] = df["yellow_cards"] + df["red_cards"]
        df["cards_against"] = df["yellow_cards_against"] + df["red_cards_against"]
    return df.sort_values(["date", "game_id"]).reset_index(drop=True)


def srednie_ligi(df: pd.DataFrame) -> dict:
    dom = df[df["venue"] == "home"]
    if dom.empty:
        raise ValueError("Brak rozegranych meczów w danych — nie da się policzyć średnich ligi.")
    return {
        "gole_gosp": float(dom["goals"].mean()),
        "gole_gosci": float(dom["goals_against"].mean()),
        "mecze": len(dom),
    }


def _mieszaj(seria: pd.Series, n_forma: int) -> float:
    """60% średnia sezonu + 40% średnia z ostatnich n_forma meczów (pomija braki)."""
    s = seria.dropna()
    if s.empty:
        return float("nan")
    return WAGA_SEZONU * float(s.mean()) + (1 - WAGA_SEZONU) * float(s.tail(n_forma).mean())


def metryka_goli(wiersze: pd.DataFrame, kol_xg: str, kol_gole: str, n_forma: int) -> tuple:
    """Na mecz: 0.7 × xG + 0.3 × gole (brak xG -> same gole); potem sezon/forma 60/40."""
    xg, gole = wiersze[kol_xg], wiersze[kol_gole]
    braki = int(xg.isna().sum())
    na_mecz = WAGA_XG * xg.fillna(gole) + (1 - WAGA_XG) * gole
    return _mieszaj(na_mecz, n_forma), len(wiersze), braki


def wspolczynnik(wartosc: float, srednia: float, n: int, prior: float = 1.0) -> float:
    if n == 0 or math.isnan(wartosc) or srednia <= 0:
        return prior
    surowy = wartosc / srednia
    return prior + (surowy - prior) * n / (n + K_SCIAGANIA)


def wspolczynniki_druzyny(df: pd.DataFrame, druzyna: str, n_forma: int, sr: dict) -> dict:
    """Surowe składniki współczynników drużyny u siebie i na wyjeździe."""
    out = {}
    for strona, sr_za, sr_przeciw in (
        ("dom", sr["gole_gosp"], sr["gole_gosci"]),
        ("wyjazd", sr["gole_gosci"], sr["gole_gosp"]),
    ):
        w = df[(df["team"] == druzyna) & (df["venue"] == ("home" if strona == "dom" else "away"))]
        atak, n, braki_a = metryka_goli(w, "xg", "goals", n_forma)
        obrona, _, braki_o = metryka_goli(w, "xg_against", "goals_against", n_forma)
        out[strona] = {
            "atak": atak,
            "obrona": obrona,
            "n": n,
            "sr_za": sr_za,
            "sr_przeciw": sr_przeciw,
            "braki_xg": braki_a + braki_o,
        }
    return out


def prior_z_sezonu(df_prev: pd.DataFrame, druzyna: str, n_forma: int) -> dict | None:
    """Współczynniki z poprzedniego sezonu, ściągnięte w połowie do 1.0 (transfery, regresja)."""
    if druzyna not in set(df_prev["team"]):
        return None
    sr = srednie_ligi(df_prev)
    skl = wspolczynniki_druzyny(df_prev, druzyna, n_forma, sr)
    prior = {}
    for strona, s in skl.items():
        for kto in ("atak", "obrona"):
            sred = s["sr_za"] if kto == "atak" else s["sr_przeciw"]
            prior[f"{kto}_{strona}"] = 1 + 0.5 * (wspolczynnik(s[kto], sred, s["n"]) - 1)
    return prior


def oblicz_lambdy(
    df: pd.DataFrame,
    gosp: str,
    gosc: str,
    n_forma: int = 6,
    priory: dict | None = None,
) -> dict:
    """λ gospodarza i gościa według wzoru ze skilla + lista jawnych założeń."""
    priory = priory or {}
    sr = srednie_ligi(df)
    g = wspolczynniki_druzyny(df, gosp, n_forma, sr)["dom"]
    a = wspolczynniki_druzyny(df, gosc, n_forma, sr)["wyjazd"]
    pg, pa = priory.get(gosp) or {}, priory.get(gosc) or {}
    atak_g = wspolczynnik(g["atak"], sr["gole_gosp"], g["n"], pg.get("atak_dom", 1.0))
    obrona_g = wspolczynnik(g["obrona"], sr["gole_gosci"], g["n"], pg.get("obrona_dom", 1.0))
    atak_a = wspolczynnik(a["atak"], sr["gole_gosci"], a["n"], pa.get("atak_wyjazd", 1.0))
    obrona_a = wspolczynnik(a["obrona"], sr["gole_gosp"], a["n"], pa.get("obrona_wyjazd", 1.0))
    lh = atak_g * obrona_a * sr["gole_gosp"]
    la = atak_a * obrona_g * sr["gole_gosci"]

    zal = [
        (
            f"Średnie ligi z {sr['mecze']} meczów: gospodarze {sr['gole_gosp']:.2f}, "
            f"goście {sr['gole_gosci']:.2f} gola na mecz."
        ),
        "xG z Sofascore zawiera karne — bez osobnej korekty na karne.",
    ]
    for druzyna, s, strona, p in ((gosp, g, "u siebie", pg), (gosc, a, "na wyjeździe", pa)):
        waga = s["n"] / (s["n"] + K_SCIAGANIA) if s["n"] else 0.0
        cel = "współczynników z zeszłego sezonu" if p else "średniej ligi"
        zal.append(
            f"{druzyna} {strona}: {s['n']} meczów → waga danych {waga:.0%}, reszta z {cel}."
        )
        if s["n"] < 8 and not p:
            zal.append(
                f"{druzyna}: mała próba {strona} — rozważ --poprzedni-sezon i wyższy próg pewności."
            )
        if s["braki_xg"]:
            zal.append(f"{druzyna}: w {s['braki_xg']} pozycjach brak xG — użyte same gole.")
    return {
        "lh": round(lh, 3),
        "la": round(la, 3),
        "wspolczynniki": {
            "atak_gosp": round(atak_g, 3),
            "obrona_gosp": round(obrona_g, 3),
            "atak_gosc": round(atak_a, 3),
            "obrona_gosc": round(obrona_a, 3),
        },
        "srednie_ligi": sr,
        "zalozenia": zal,
    }


def _var_ratio(sumy: pd.Series, domyslne: float) -> tuple[float, bool]:
    s = sumy.dropna()
    if len(s) < MIN_MECZOW_VAR or s.mean() <= 0:
        return domyslne, False
    return round(min(max(float(s.var(ddof=1) / s.mean()), 1.0), 2.0), 2), True


def oblicz_statystyki(
    df: pd.DataFrame,
    gosp: str,
    gosc: str,
    sedzia: dict | None = None,
    n_forma: int = 6,
) -> tuple[dict, list[str]]:
    """Rynki statystyczne do mecz.json: {nazwa: {gosp, gosc, var_ratio}} i kartki {suma}."""
    dom = df[(df["team"] == gosp) & (df["venue"] == "home")]
    wyj = df[(df["team"] == gosc) & (df["venue"] == "away")]
    mecze = df[df["venue"] == "home"]
    stat, zal = {}, []
    for nazwa, kol in STATY_STRON.items():
        if kol not in df.columns:
            continue
        wg = [_mieszaj(dom[kol], n_forma), _mieszaj(wyj[f"{kol}_against"], n_forma)]
        wa = [_mieszaj(wyj[kol], n_forma), _mieszaj(dom[f"{kol}_against"], n_forma)]
        wg, wa = [x for x in wg if not math.isnan(x)], [x for x in wa if not math.isnan(x)]
        if not wg or not wa:
            zal.append(f"{nazwa}: brak danych dla jednej z drużyn — rynek pominięty.")
            continue
        vr, z_danych = _var_ratio(mecze[kol] + mecze[f"{kol}_against"], VAR_RATIO_DOMYSLNE[kol])
        stat[nazwa] = {
            "gosp": round(sum(wg) / len(wg), 2),
            "gosc": round(sum(wa) / len(wa), 2),
            "var_ratio": vr,
        }
        if not z_danych:
            zal.append(f"{nazwa}: var_ratio domyślne {vr} (za mało meczów w danych).")

    if "cards" in df.columns:

        def kartki_druzyny(d: str) -> float:
            return _mieszaj(df[df["team"] == d]["cards"], n_forma)

        druzyny = kartki_druzyny(gosp) + kartki_druzyny(gosc)
        if not math.isnan(druzyny):
            suma, opis = druzyny, "same drużyny (brak sędziego)"
            if sedzia and sedzia.get("mecze") and sedzia.get("kartki_na_mecz") is not None:
                w = 0.5 if sedzia["mecze"] >= 8 else 0.3
                suma = w * sedzia["kartki_na_mecz"] + (1 - w) * druzyny
                opis = (
                    f"{w:.0%} sędzia ({sedzia['kartki_na_mecz']:.2f}/mecz) + {1 - w:.0%} drużyny"
                )
            vr, _ = _var_ratio(mecze["cards"] + mecze["cards_against"], VAR_RATIO_KARTKI)
            stat["Kartki"] = {"suma": round(suma, 2), "var_ratio": vr}
            zal.append(
                f"Kartki: {opis}; drużyny łącznie {druzyny:.2f}/mecz. Liczone żółte + czerwone"
                " po 1 — sprawdź regulamin rynku (czerwona za 2?)."
            )
    return stat, zal


# ---------- kontekst i ciekawostki ----------


def _wynik(r: pd.Series) -> str:
    g, s = int(r["goals"]), int(r["goals_against"])
    return "W" if g > s else ("R" if g == s else "P")


def forma(df: pd.DataFrame, druzyna: str, n: int = 5) -> list[str]:
    out = []
    for _, r in df[df["team"] == druzyna].tail(n).iterrows():
        xg = (
            f", xG {r['xg']:.2f}–{r['xg_against']:.2f}"
            if not (pd.isna(r["xg"]) or pd.isna(r["xg_against"]))
            else ""
        )
        gdzie = "dom" if r["venue"] == "home" else "wyjazd"
        dzien = r["date"].tz_convert(WARSZAWA).strftime("%d.%m")
        out.append(
            f"{dzien} {_wynik(r)} {int(r['goals'])}:{int(r['goals_against'])} "
            f"vs {r['opponent']} ({gdzie}{xg})"
        )
    return out


def _odsetek(maska: pd.Series) -> str:
    return f"{int(maska.sum())}/{len(maska)}"


def ciekawostki(df: pd.DataFrame, gosp: str, gosc: str) -> list[str]:
    """Liczbowe ciekawostki z sezonu — do sekcji kontekstu w raporcie."""
    out = []
    for druzyna, venue, gdzie in ((gosp, "home", "u siebie"), (gosc, "away", "na wyjeździe")):
        w = df[(df["team"] == druzyna) & (df["venue"] == venue)]
        if w.empty:
            continue
        suma = w["goals"] + w["goals_against"]
        out.append(
            f"{druzyna} {gdzie} ({len(w)} m.): Over 2.5 {_odsetek(suma > 2.5)}, "
            f"BTTS {_odsetek((w['goals'] > 0) & (w['goals_against'] > 0))}, "
            f"czyste konto {_odsetek(w['goals_against'] == 0)}, "
            f"bez gola {_odsetek(w['goals'] == 0)}."
        )
        if w["xg"].notna().any():
            out.append(
                f"{druzyna} {gdzie}: śr. xG {w['xg'].mean():.2f}, xGA {w['xg_against'].mean():.2f}"
                f" | gole {w['goals'].mean():.2f}–{w['goals_against'].mean():.2f}."
            )
        if "corners" in w and w["corners"].notna().any():
            lacznie = (w["corners"] + w["corners_against"]).mean()
            out.append(
                f"{druzyna} {gdzie}: rożne {w['corners'].mean():.1f} wywalczone, "
                f"{w['corners_against'].mean():.1f} oddane, łącznie {lacznie:.1f}/mecz."
            )
    for druzyna in (gosp, gosc):
        w = df[df["team"] == druzyna]
        z_xg = w.dropna(subset=["xg", "xg_against"])
        if len(z_xg) >= 5:
            atak = z_xg["goals"].sum() - z_xg["xg"].sum()
            obrona = z_xg["goals_against"].sum() - z_xg["xg_against"].sum()
            if abs(atak) >= 3:
                kier = "ponad" if atak > 0 else "poniżej"
                out.append(
                    f"{druzyna} strzela {abs(atak):.1f} gola {kier} xG w {len(z_xg)} meczach"
                    " — skuteczność zwykle wraca do średniej."
                )
            if abs(obrona) >= 3:
                kier = "więcej" if obrona > 0 else "mniej"
                out.append(
                    f"{druzyna} traci {abs(obrona):.1f} gola {kier}, niż wynika z xGA"
                    f" ({len(z_xg)} meczów)."
                )
        if "cards" in w and w["cards"].notna().any():
            out.append(
                f"{druzyna}: {w['cards'].mean():.2f} kartki/mecz, rywale {w['cards_against'].mean():.2f}."
            )
        seria = "".join(_wynik(r) for _, r in w.tail(5).iterrows())
        if seria:
            out.append(f"{druzyna} — ostatnie {len(seria)}: {' '.join(seria)}.")
    h2h = df[(df["team"] == gosp) & (df["opponent"] == gosc)]
    for _, r in h2h.iterrows():
        gdzie = "u siebie" if r["venue"] == "home" else "na wyjeździe"
        out.append(
            f"W tym sezonie: {gosp} {gdzie} {int(r['goals'])}:{int(r['goals_against'])} z {gosc}."
        )
    return out


def odpoczynek(df: pd.DataFrame, druzyna: str, data_meczu: datetime) -> int | None:
    w = df[(df["team"] == druzyna) & (df["date"] < data_meczu)]
    if w.empty:
        return None
    return int((data_meczu - w["date"].iloc[-1]).total_seconds() // 86400)


def zbuduj(
    df: pd.DataFrame,
    gosp: str,
    gosc: str,
    liga: str,
    data_meczu: datetime | None = None,
    sedzia: dict | None = None,
    absencje: list[str] | None = None,
    priory: dict | None = None,
    n_forma: int = 6,
    zrodla: list[dict] | None = None,
) -> dict:
    """Złóż mecz.json: wejście modelu + kontekst, ciekawostki, założenia i źródła."""
    df = przygotuj(df)
    if data_meczu is not None:
        df = df[df["date"] < data_meczu]  # tylko mecze rozegrane przed tym meczem
    lam = oblicz_lambdy(df, gosp, gosc, n_forma, priory)
    stat, zal_stat = oblicz_statystyki(df, gosp, gosc, sedzia, n_forma)
    kiedy = data_meczu.astimezone(WARSZAWA).strftime("%d.%m %H:%M") if data_meczu else "termin ?"
    kontekst = {
        "forma_gosp": forma(df, gosp),
        "forma_gosc": forma(df, gosc),
        "ciekawostki": ciekawostki(df, gosp, gosc),
        "wspolczynniki": lam["wspolczynniki"],
        "srednie_ligi": lam["srednie_ligi"],
    }
    if data_meczu is not None:
        kontekst["dni_odpoczynku"] = {
            gosp: odpoczynek(df, gosp, data_meczu),
            gosc: odpoczynek(df, gosc, data_meczu),
            "uwaga": "tylko mecze ligowe — puchary i kadra sprawdź osobno",
        }
    if sedzia:
        kontekst["sedzia"] = sedzia
    if absencje is not None:
        kontekst["absencje"] = absencje or ["Sofascore nie podaje jeszcze braków kadrowych."]
    return {
        "mecz": f"{gosp} – {gosc}, {liga}, {kiedy}",
        "model": {"lh": lam["lh"], "la": lam["la"]},
        "rho": -0.10,
        "statystyki": stat,
        "kontekst": kontekst,
        "zalozenia": lam["zalozenia"] + zal_stat,
        "zrodla": zrodla or [],
        "korekty": [],
        "wersja": "analityk-meczowy-v2",
    }


# ---------- pobieranie (Sofascore przez fork soccerdata) ----------


def sezon_biezacy(teraz: datetime) -> str:
    """Ekstraklasa gra lipiec–maj: listopad 2026 -> '26-27', marzec 2027 -> '26-27'."""
    rok = teraz.year % 100 if teraz.month >= 7 else (teraz.year - 1) % 100
    return f"{rok:02d}-{(rok + 1) % 100:02d}"


def sezon_poprzedni(sezon: str) -> str:
    a = int(sezon[:2])
    return f"{(a - 1) % 100:02d}-{a:02d}"


def _zapewnij_tls() -> None:
    """soccerdata pobiera bibliotekę TLS przy pierwszym użyciu; w części środowisk linuksowych
    automatyczne pobranie nie trafia w nazwę pliku — wtedy ściągamy build 'ubuntu' bezpośrednio."""
    try:
        from tls_requests.models.libraries import BIN_DIR, LATEST_VERSION_TAG_NAME, TLSLibrary
    except ImportError:
        return
    try:
        TLSLibrary.load()
        return
    except OSError:
        if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
            raise
    wersja = LATEST_VERSION_TAG_NAME.lstrip("v")
    plik = f"tls-client-linux-ubuntu-amd64-{wersja}.so"
    url = f"https://github.com/bogdanfinn/tls-client/releases/download/v{wersja}/{plik}"
    print(f"Pobieram bibliotekę TLS: {url}", file=sys.stderr)
    urllib.request.urlretrieve(url, os.path.join(BIN_DIR, plik))
    TLSLibrary.load()


def _liczba(x: object) -> float:
    """Wartość liczbowa albo 0 dla None/NaN (pola sędziego bywają puste)."""
    return float(x) if x is not None and pd.notna(x) else 0.0


def _absencje_tekst(df: pd.DataFrame, gosp: str, gosc: str) -> list[str]:
    out = []
    for _, r in df.iterrows():
        druzyna = gosp if r["side"] == "home" else gosc
        powrot = f", powrót ~{r['expected_return']}" if pd.notna(r["expected_return"]) else ""
        opis = r["description"] if pd.notna(r["description"]) else "bez opisu"
        out.append(f"{druzyna}: {r['player']} ({r['position']}) — {opis}{powrot} [{r['type']}]")
    return out


def pobierz(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    _zapewnij_tls()
    import soccerdata as sd

    teraz = datetime.now(tz=timezone.utc)
    sezon = args.sezon or sezon_biezacy(teraz)
    rd = sd.Sofascore(args.liga, sezon)
    harm = rd.read_schedule().reset_index()
    druzyny = sorted(set(harm["home_team"]) | set(harm["away_team"]))
    gosp, gosc = dopasuj(args.gospodarz, druzyny), dopasuj(args.gosc, druzyny)
    zalozenia = []

    def nierozegrane(h: str, a: str) -> pd.DataFrame:
        m = (harm["home_team"] == h) & (harm["away_team"] == a) & harm["home_score"].isna()
        return harm[m].sort_values("date")

    przyszle = nierozegrane(gosp, gosc)
    if przyszle.empty and not nierozegrane(gosc, gosp).empty:
        gosp, gosc = gosc, gosp
        przyszle = nierozegrane(gosp, gosc)
        zalozenia.append(f"Zamieniono kolejność: gospodarzem jest {gosp}.")
    if przyszle.empty:
        raise SystemExit(f"Brak nierozegranego meczu {gosp} – {gosc} w sezonie {sezon}.")
    mecz = przyszle.iloc[0]
    game_id = int(mecz["game_id"])

    staty = rd.read_team_match_stats()
    szczegoly = rd.read_event_details(game_id).iloc[0]
    sedzia = None
    if pd.notna(szczegoly.get("referee")) and szczegoly.get("referee"):
        mecze = int(_liczba(szczegoly.get("referee_games")))
        kartki = _liczba(szczegoly.get("referee_yellow_cards")) + _liczba(
            szczegoly.get("referee_red_cards")
        )
        sedzia = {
            "nazwisko": szczegoly["referee"],
            "mecze": mecze,
            "kartki_na_mecz": round(kartki / mecze, 2) if mecze else None,
            "uwaga": "statystyki kariery z Sofascore (wszystkie rozgrywki)",
        }
    else:
        zalozenia.append("Sędzia jeszcze nieznany — kartki liczone z samych drużyn.")
    absencje = _absencje_tekst(rd.read_missing_players(game_id), gosp, gosc)

    priory = None
    if args.poprzedni_sezon:
        prev = przygotuj(sd.Sofascore(args.liga, sezon_poprzedni(sezon)).read_team_match_stats())
        priory = {d: prior_z_sezonu(prev, d, args.forma) for d in (gosp, gosc)}
        for d, p in priory.items():
            if p is None:
                zalozenia.append(f"{d}: brak w poprzednim sezonie ligi (beniaminek?) — prior 1.0.")

    pobrano = teraz.astimezone(WARSZAWA).strftime("%Y-%m-%d %H:%M")
    zrodla = [
        {"zrodlo": "Sofascore (fork PhysiotoIT/soccerdata)", "sezon": sezon, "pobrano": pobrano},
        {"mecz": f"https://www.sofascore.com/event/{game_id}", "game_id": game_id},
    ]
    meta = {
        "gosp": gosp,
        "gosc": gosc,
        "data": mecz["date"].to_pydatetime(),
        "sedzia": sedzia,
        "absencje": absencje,
        "priory": priory,
        "zrodla": zrodla,
        "zalozenia": zalozenia,
    }
    return staty, meta


def lista_meczow(args: argparse.Namespace) -> None:
    _zapewnij_tls()
    import soccerdata as sd

    sezon = args.sezon or sezon_biezacy(datetime.now(tz=timezone.utc))
    harm = sd.Sofascore(args.liga, sezon).read_schedule().reset_index()
    druzyna = dopasuj(args.lista, sorted(set(harm["home_team"]) | set(harm["away_team"])))
    m = ((harm["home_team"] == druzyna) | (harm["away_team"] == druzyna)) & harm[
        "home_score"
    ].isna()
    for _, r in harm[m].sort_values("date").head(5).iterrows():
        kiedy = r["date"].tz_convert(WARSZAWA).strftime("%d.%m %H:%M")
        print(f"{kiedy}  {r['home_team']} – {r['away_team']}  (game_id {r['game_id']})")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("gospodarz", nargs="?")
    p.add_argument("gosc", nargs="?")
    p.add_argument("--liga", default="POL-Ekstraklasa")
    p.add_argument("--sezon", help="np. 26-27 (domyślnie bieżący)")
    p.add_argument("--forma", type=int, default=6, help="ile ostatnich meczów to forma (6)")
    p.add_argument("--wyjscie", default="mecz.json")
    p.add_argument("--poprzedni-sezon", action="store_true", help="prior z zeszłego sezonu")
    p.add_argument("--lista", metavar="DRUŻYNA", help="pokaż najbliższe mecze drużyny")
    p.add_argument("--eksport", metavar="CSV", help="zapisz statystyki sezonu do CSV")
    p.add_argument("--z-csv", metavar="CSV", help="licz z CSV (z --eksport), bez pobierania")
    p.add_argument("--data", help="przy --z-csv: termin meczu, np. 2026-11-08T17:30")
    p.add_argument("--sedzia-mecze", type=int, help="przy --z-csv: mecze sędziego")
    p.add_argument("--sedzia-kartki", type=float, help="przy --z-csv: kartki sędziego na mecz")
    args = p.parse_args(argv)

    if args.lista:
        try:
            lista_meczow(args)
        except (ConnectionError, OSError) as e:
            raise SystemExit(BRAK_DOSTEPU.format(blad=e)) from None
        return
    if not (args.gospodarz and args.gosc):
        p.error("podaj gospodarza i gościa (albo --lista DRUŻYNA)")

    if args.z_csv:
        staty = pd.read_csv(args.z_csv)
        druzyny = sorted(set(staty["team"]))
        data = None
        if args.data:
            data = datetime.fromisoformat(args.data)
            data = data.replace(tzinfo=WARSZAWA) if data.tzinfo is None else data
        sedzia = None
        if args.sedzia_kartki is not None:
            sedzia = {
                "nazwisko": "?",
                "mecze": args.sedzia_mecze or 0,
                "kartki_na_mecz": args.sedzia_kartki,
            }
        meta = {
            "gosp": dopasuj(args.gospodarz, druzyny),
            "gosc": dopasuj(args.gosc, druzyny),
            "data": data,
            "sedzia": sedzia,
            "absencje": None,
            "priory": None,
            "zrodla": [{"zrodlo": f"CSV {os.path.basename(args.z_csv)}"}],
            "zalozenia": [],
        }
    else:
        try:
            staty, meta = pobierz(args)
        except (ConnectionError, OSError) as e:
            raise SystemExit(BRAK_DOSTEPU.format(blad=e)) from None
    if args.eksport:
        przygotuj(staty).to_csv(args.eksport, index=False)

    wynik = zbuduj(
        staty,
        meta["gosp"],
        meta["gosc"],
        args.liga,
        data_meczu=meta["data"],
        sedzia=meta["sedzia"],
        absencje=meta["absencje"],
        priory=meta["priory"],
        n_forma=args.forma,
        zrodla=meta["zrodla"],
    )
    wynik["zalozenia"] = meta["zalozenia"] + wynik["zalozenia"]
    with open(args.wyjscie, "w", encoding="utf-8") as fh:
        json.dump(wynik, fh, ensure_ascii=False, indent=1, default=str)
    print(f"Zapisano {args.wyjscie}: {wynik['mecz']}")
    print(f"λ {wynik['model']['lh']:.2f} – {wynik['model']['la']:.2f}")
    for z in wynik["zalozenia"]:
        print(f"- {z}")


if __name__ == "__main__":
    main()
