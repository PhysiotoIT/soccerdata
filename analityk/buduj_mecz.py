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
import numbers
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
K_LIGI = 30  # średnie ligi ściągane do zeszłego sezonu wagą n / (n + K_LIGI)
MIN_DRUZYN_CSV = 10
KOLUMNY_WYMAGANE = ("date", "team", "opponent", "venue", "goals", "goals_against")
BRAK_DOSTEPU = """Nie udało się pobrać danych z Sofascore ({blad}).
Możliwe przyczyny i co zrobić:
- sandbox Claude: dodaj api.sofascore.com do dozwolonych domen (Ustawienia > Capabilities)
  albo uruchom skrypt na swoim komputerze;
- Sofascore blokuje adres IP (403): spróbuj później albo z innej sieci;
- dane z innego miejsca: zapisz je w CSV i użyj --z-csv (kolumny w SKILL.md)."""


class BladUzytkownika(ValueError):
    """Błąd do pokazania użytkownikowi bez śladu stosu (zła nazwa, brak danych, zły sezon)."""


# ---------- dopasowanie nazw ----------


def _tokeny(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", str(s).translate(str.maketrans("łŁ", "lL")))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()


def dopasuj(nazwa: str, druzyny: list[str]) -> str:
    """Zwróć drużynę z listy pasującą do wpisanej nazwy ("lech", "Rakow", "Legia W.").

    Kolejno: identyczna nazwa, wszystkie słowa jako całe słowa ("Lech" → Lech Poznań, nie
    Lechia Gdańsk), wreszcie początki słów ("Rak" → Raków Częstochowa).
    """
    q = [x.rstrip(".") for x in _tokeny(nazwa)]
    if not q:
        raise BladUzytkownika("Pusta nazwa drużyny.")
    for pasuje in (
        lambda t: t == q,
        lambda t: all(x in t for x in q),
        lambda t: all(any(s.startswith(x) for s in t) for x in q),
    ):
        kandydaci = [d for d in druzyny if pasuje(_tokeny(d))]
        if len(kandydaci) == 1:
            return kandydaci[0]
        if kandydaci:
            raise BladUzytkownika(
                f"„{nazwa}” pasuje do kilku drużyn: {', '.join(sorted(kandydaci))}"
            )
    raise BladUzytkownika(
        f"Nie znam drużyny „{nazwa}”. Drużyny w danych: {', '.join(sorted(druzyny))}"
    )


# ---------- dane ----------


def przygotuj(df: pd.DataFrame) -> pd.DataFrame:
    """Spłaszcz wynik Sofascore.read_team_match_stats (albo CSV) i posortuj po dacie.

    Wymagane kolumny: date, team, opponent, venue (home/away), goals, goals_against —
    wiersz na drużynę i mecz. Reszta (xg, corners, yellow_cards, ... i ich *_against)
    jest opcjonalna; brak xG oznacza liczenie z samych goli.
    """
    df = df.reset_index() if "team" not in df.columns else df.copy()
    brak = [k for k in KOLUMNY_WYMAGANE if k not in df.columns]
    if brak:
        raise BladUzytkownika(f"W danych brakuje kolumn: {', '.join(brak)}.")
    df["date"] = pd.to_datetime(df["date"], utc=True)
    for kol in ("xg", "xg_against"):
        if kol not in df.columns:
            df[kol] = float("nan")
    if {"yellow_cards", "red_cards", "yellow_cards_against", "red_cards_against"} <= set(df):
        df["cards"] = df["yellow_cards"] + df["red_cards"]
        df["cards_against"] = df["yellow_cards_against"] + df["red_cards_against"]
    klucz = ["date", "game_id"] if "game_id" in df.columns else ["date", "team"]
    return df.sort_values(klucz).reset_index(drop=True)


def _metryka(w: pd.DataFrame, kol_xg: str, kol_gole: str) -> pd.Series:
    """Na mecz: 0.7 × xG + 0.3 × gole (brak xG -> same gole)."""
    return WAGA_XG * w[kol_xg].fillna(w[kol_gole]) + (1 - WAGA_XG) * w[kol_gole]


def srednie_ligi(df: pd.DataFrame, poprzednie: dict | None = None) -> dict:
    """Średnie ligi: gole (do λ) i ta sama metryka xG/gole co dla drużyn (do współczynników).

    Z ``poprzednie`` (średnie zeszłego sezonu) wynik jest ściągany do nich wagą
    n / (n + K_LIGI) — na starcie sezonu bieżące średnie to szum.
    """
    dom = df[df["venue"] == "home"]
    n = len(dom)
    if n == 0 and poprzednie is None:
        raise BladUzytkownika("Brak rozegranych meczów w sezonie — uruchom z --poprzedni-sezon.")
    klucze = ("gole_gosp", "gole_gosci", "metryka_gosp", "metryka_gosci")
    biezace = dict.fromkeys(klucze, 0.0)
    if n:
        biezace = {
            "gole_gosp": float(dom["goals"].mean()),
            "gole_gosci": float(dom["goals_against"].mean()),
            "metryka_gosp": float(_metryka(dom, "xg", "goals").mean()),
            "metryka_gosci": float(_metryka(dom, "xg_against", "goals_against").mean()),
        }
    if poprzednie is None:
        return {**biezace, "mecze": n}
    w = n / (n + K_LIGI)
    out = {k: w * biezace[k] + (1 - w) * poprzednie[k] for k in klucze}
    return {**out, "mecze": n, "waga_sezonu": round(w, 2)}


def _mieszaj(seria: pd.Series, n_forma: int) -> float:
    """60% średnia sezonu + 40% średnia z ostatnich n_forma meczów (pomija braki)."""
    s = seria.dropna()
    if s.empty:
        return float("nan")
    return WAGA_SEZONU * float(s.mean()) + (1 - WAGA_SEZONU) * float(s.tail(n_forma).mean())


def metryka_goli(wiersze: pd.DataFrame, kol_xg: str, kol_gole: str, n_forma: int) -> tuple:
    """Na mecz: 0.7 × xG + 0.3 × gole (brak xG -> same gole); potem sezon/forma 60/40."""
    braki = int(wiersze[kol_xg].isna().sum())
    return _mieszaj(_metryka(wiersze, kol_xg, kol_gole), n_forma), len(wiersze), braki


def wspolczynnik(wartosc: float, srednia: float, n: int, prior: float = 1.0) -> float:
    if n == 0 or math.isnan(wartosc) or srednia <= 0:
        return prior
    surowy = wartosc / srednia
    return prior + (surowy - prior) * n / (n + K_SCIAGANIA)


def wspolczynniki_druzyny(df: pd.DataFrame, druzyna: str, n_forma: int, sr: dict) -> dict:
    """Surowe składniki współczynników drużyny u siebie i na wyjeździe.

    sr_za / sr_przeciw to średnia ligi tej samej metryki (xG/gole 70/30), więc współczynnik
    1.0 = poziom ligi, nawet gdy xG ligi różni się od liczby goli.
    """
    out = {}
    for strona, sr_za, sr_przeciw in (
        ("dom", sr["metryka_gosp"], sr["metryka_gosci"]),
        ("wyjazd", sr["metryka_gosci"], sr["metryka_gosp"]),
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
    srednie_poprzednie: dict | None = None,
) -> dict:
    """λ gospodarza i gościa według wzoru ze skilla + lista jawnych założeń."""
    priory = priory or {}
    sr = srednie_ligi(df, srednie_poprzednie)
    g = wspolczynniki_druzyny(df, gosp, n_forma, sr)["dom"]
    a = wspolczynniki_druzyny(df, gosc, n_forma, sr)["wyjazd"]
    pg, pa = priory.get(gosp) or {}, priory.get(gosc) or {}
    atak_g = wspolczynnik(g["atak"], g["sr_za"], g["n"], pg.get("atak_dom", 1.0))
    obrona_g = wspolczynnik(g["obrona"], g["sr_przeciw"], g["n"], pg.get("obrona_dom", 1.0))
    atak_a = wspolczynnik(a["atak"], a["sr_za"], a["n"], pa.get("atak_wyjazd", 1.0))
    obrona_a = wspolczynnik(a["obrona"], a["sr_przeciw"], a["n"], pa.get("obrona_wyjazd", 1.0))
    lh = atak_g * obrona_a * sr["gole_gosp"]
    la = atak_a * obrona_g * sr["gole_gosci"]

    skad = f"{sr['mecze']} meczów"
    if "waga_sezonu" in sr:
        skad += f", ściągnięte do zeszłego sezonu (waga bieżącego {sr['waga_sezonu']:.0%})"
    zal = [
        (
            f"Średnie ligi ({skad}): gospodarze {sr['gole_gosp']:.2f}, "
            f"goście {sr['gole_gosci']:.2f} gola na mecz."
        ),
        "Współczynniki = metryka drużyny / ta sama metryka (xG/gole 70/30) średnio w lidze.",
        "xG z Sofascore zawiera karne — bez osobnej korekty na karne.",
    ]
    for druzyna, s, strona, p in ((gosp, g, "u siebie", pg), (gosc, a, "na wyjeździe", pa)):
        waga = s["n"] / (s["n"] + K_SCIAGANIA) if s["n"] else 0.0
        cel = "współczynniki z zeszłego sezonu" if p else "średnia ligi"
        zal.append(f"{druzyna} {strona}: {s['n']} meczów → waga danych {waga:.0%}, reszta: {cel}.")
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
    srednie_poprzednie: dict | None = None,
) -> dict:
    """Złóż mecz.json: wejście modelu + kontekst, ciekawostki, założenia i źródła."""
    df = przygotuj(df)
    if data_meczu is not None:
        df = df[df["date"] < data_meczu]  # tylko mecze rozegrane przed tym meczem
    lam = oblicz_lambdy(df, gosp, gosc, n_forma, priory, srednie_poprzednie)
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


def normalizuj_sezon(sezon: str) -> str:
    """'26-27', '2026-27', '2026/2027', '2627' -> '26-27'."""
    m = re.fullmatch(r"(?:\d\d)?(\d\d)\s*[-/]?\s*(?:\d\d)?(\d\d)", sezon.strip())
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise BladUzytkownika(f"Nie rozumiem sezonu „{sezon}” — podaj np. 26-27.")
    return f"{m.group(1)}-{m.group(2)}"


def sezon_poprzedni(sezon: str) -> str:
    a = int(normalizuj_sezon(sezon)[:2])
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
    except OSError as e:
        if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
            raise BladUzytkownika(f"Nie udało się załadować biblioteki TLS ({e}).") from e
    wersja = LATEST_VERSION_TAG_NAME.lstrip("v")
    plik = f"tls-client-linux-ubuntu-amd64-{wersja}.so"
    url = f"https://github.com/bogdanfinn/tls-client/releases/download/v{wersja}/{plik}"
    print(f"Pobieram bibliotekę TLS: {url}", file=sys.stderr)
    try:
        urllib.request.urlretrieve(url, os.path.join(BIN_DIR, plik))
        TLSLibrary.load()
    except OSError as e:
        raise BladUzytkownika(
            f"Nie udało się pobrać biblioteki TLS z GitHuba ({e}). To nie jest problem z Sofascore:"
            " sprawdź dostęp do github.com albo pobierz plik ręcznie do " + BIN_DIR
        ) from e


def _jest_liczba(x: object) -> bool:
    """Liczba (także numpy), ale nie bool, None ani NaN."""
    return isinstance(x, numbers.Real) and not isinstance(x, bool) and not math.isnan(x)


def sedzia_z_szczegolow(sz: pd.Series) -> tuple[dict | None, str | None]:
    """Sędzia do mecz.json + ewentualne założenie. Brak statystyk kartek = sędzia nieznany
    dla rynku kartek (nie zero kartek)."""
    nazwisko = sz.get("referee")
    if not isinstance(nazwisko, str) or not nazwisko:
        return None, "Sędzia jeszcze nieznany — kartki liczone z samych drużyn."
    mecze, zolte, czerwone = (
        sz.get("referee_games"),
        sz.get("referee_yellow_cards"),
        sz.get("referee_red_cards"),
    )
    sedzia = {
        "nazwisko": nazwisko,
        "mecze": None,
        "kartki_na_mecz": None,
        "uwaga": "statystyki kariery z Sofascore (wszystkie rozgrywki)",
    }
    if not (_jest_liczba(mecze) and mecze > 0 and _jest_liczba(zolte)):
        return sedzia, f"Sędzia {nazwisko}: brak statystyk kartek — kartki z samych drużyn."
    kartki = zolte + (czerwone if _jest_liczba(czerwone) else 0)
    sedzia.update(mecze=int(mecze), kartki_na_mecz=round(kartki / mecze, 2))
    return sedzia, None


def wybierz_mecz(harm: pd.DataFrame, gosp: str, gosc: str) -> tuple[pd.Series, list[str]]:
    """Najbliższy nierozegrany mecz tych drużyn w dowolnej kolejności + założenia."""
    para = ((harm["home_team"] == gosp) & (harm["away_team"] == gosc)) | (
        (harm["home_team"] == gosc) & (harm["away_team"] == gosp)
    )
    przyszle = harm[para & harm["home_score"].isna()].sort_values("date")
    if przyszle.empty:
        raise BladUzytkownika(f"Brak nierozegranego meczu {gosp} – {gosc} w tym sezonie.")
    mecz = przyszle.iloc[0]
    zal = []
    if mecz["home_team"] != gosp:
        zal.append(f"Zamieniono kolejność: najbliższy mecz to {gosc} (dom) – {gosp}.")
    dni = (mecz["date"] - datetime.now(tz=timezone.utc)).days
    if dni > 10:
        zal.append(f"Mecz jest dopiero za {dni} dni — forma i składy mogą się zmienić.")
    return mecz, zal


def _absencje_tekst(df: pd.DataFrame, gosp: str, gosc: str) -> list[str]:
    out = []
    for _, r in df.iterrows():
        druzyna = gosp if r["side"] == "home" else gosc
        powrot = f", powrót ~{r['expected_return']}" if pd.notna(r["expected_return"]) else ""
        if pd.notna(r["description"]) and r["description"]:
            opis = r["description"]
        elif pd.notna(r["reason"]):
            opis = f"powód: kod Sofascore {r['reason']} — sprawdź na stronie meczu"
        else:
            opis = "bez opisu"
        out.append(f"{druzyna}: {r['player']} ({r['position']}) — {opis}{powrot} [{r['type']}]")
    return out


def pobierz(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    _zapewnij_tls()
    import soccerdata as sd

    teraz = datetime.now(tz=timezone.utc)
    sezon = args.sezon or sezon_biezacy(teraz)
    rd = sd.Sofascore(args.liga, sezon)
    terminarz = rd.read_schedule()
    harm = terminarz.reset_index()
    druzyny = sorted(set(harm["home_team"]) | set(harm["away_team"]))
    mecz, zalozenia = wybierz_mecz(
        harm, dopasuj(args.gospodarz, druzyny), dopasuj(args.gosc, druzyny)
    )
    gosp, gosc, game_id = mecz["home_team"], mecz["away_team"], int(mecz["game_id"])

    # terminarz już jest — read_team_match_stats nie pobiera go drugi raz
    staty = rd.read_team_match_stats(schedule=terminarz)
    sedzia, uwaga = sedzia_z_szczegolow(rd.read_event_details(game_id).iloc[0])
    if uwaga:
        zalozenia.append(uwaga)
    absencje = _absencje_tekst(rd.read_missing_players(game_id), gosp, gosc)

    priory = srednie_poprzednie = None
    rozegrane = int(harm["home_score"].notna().sum())
    if not args.poprzedni_sezon and rozegrane < K_LIGI:
        args.poprzedni_sezon = True
        zalozenia.append(
            f"Rozegrane mecze w sezonie: {rozegrane} — dołączony zeszły sezon (prior)."
        )
    if args.poprzedni_sezon:
        poprzedni = sezon_poprzedni(sezon)
        try:
            prev = przygotuj(sd.Sofascore(args.liga, poprzedni).read_team_match_stats())
        except (KeyError, ValueError, ConnectionError) as e:
            prev = None
            zalozenia.append(f"Nie udało się pobrać sezonu {poprzedni} ({e}) — bez priora.")
        if prev is not None and not prev.empty:
            srednie_poprzednie = srednie_ligi(prev)
            priory = {d: prior_z_sezonu(prev, d, args.forma) for d in (gosp, gosc)}
            for d, p in priory.items():
                if p is None:
                    zalozenia.append(f"{d}: brak w sezonie {poprzedni} (beniaminek?) — prior 1.0.")

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
        "srednie_poprzednie": srednie_poprzednie,
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


def z_csv(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    with open(args.z_csv, encoding="utf-8") as fh:
        sep = ";" if fh.readline().count(";") > 1 else ","  # polski Excel zapisuje ze średnikami
    staty = przygotuj(pd.read_csv(args.z_csv, sep=sep, decimal="," if sep == ";" else "."))
    druzyny = sorted(set(staty["team"]))
    zalozenia = []
    if len(druzyny) < MIN_DRUZYN_CSV:
        zalozenia.append(
            f"CSV ma tylko {len(druzyny)} drużyn — średnie ligi z niepełnych danych, λ mniej"
            " pewne. Do rzetelnych liczb CSV powinien mieć wszystkie mecze ligi."
        )
    data = None
    if args.data:
        data = datetime.fromisoformat(args.data)
        data = data.replace(tzinfo=WARSZAWA) if data.tzinfo is None else data
    sedzia = None
    if args.sedzia_kartki is not None:
        sedzia = {
            "nazwisko": "?",
            "mecze": args.sedzia_mecze,
            "kartki_na_mecz": args.sedzia_kartki,
        }
    meta = {
        "gosp": dopasuj(args.gospodarz, druzyny),
        "gosc": dopasuj(args.gosc, druzyny),
        "data": data,
        "sedzia": sedzia,
        "absencje": None,
        "priory": None,
        "srednie_poprzednie": None,
        "zrodla": [{"zrodlo": f"CSV {os.path.basename(args.z_csv)}"}],
        "zalozenia": zalozenia,
    }
    return staty, meta


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
    p.add_argument("--z-csv", metavar="CSV", help="licz z CSV, bez pobierania")
    p.add_argument("--data", help="przy --z-csv: termin meczu, np. 2026-11-08T17:30")
    p.add_argument("--sedzia-mecze", type=int, help="przy --z-csv: mecze sędziego")
    p.add_argument("--sedzia-kartki", type=float, help="przy --z-csv: kartki sędziego na mecz")
    args = p.parse_args(argv)
    if (args.sedzia_mecze is None) != (args.sedzia_kartki is None):
        p.error("--sedzia-mecze i --sedzia-kartki podaj razem")
    if not args.lista and not (args.gospodarz and args.gosc):
        p.error("podaj gospodarza i gościa (albo --lista DRUŻYNA)")

    try:
        if args.sezon:
            args.sezon = normalizuj_sezon(args.sezon)
        if args.lista:
            lista_meczow(args)
            return
        staty, meta = z_csv(args) if args.z_csv else pobierz(args)
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
            srednie_poprzednie=meta["srednie_poprzednie"],
        )
    except BladUzytkownika as e:
        raise SystemExit(str(e)) from None
    except (ConnectionError, OSError) as e:
        raise SystemExit(BRAK_DOSTEPU.format(blad=e)) from None
    wynik["zalozenia"] = meta["zalozenia"] + wynik["zalozenia"]
    with open(args.wyjscie, "w", encoding="utf-8") as fh:
        json.dump(wynik, fh, ensure_ascii=False, indent=1, default=str)
    print(f"Zapisano {args.wyjscie}: {wynik['mecz']}")
    print(f"λ {wynik['model']['lh']:.2f} – {wynik['model']['la']:.2f}")
    for z in wynik["zalozenia"]:
        print(f"- {z}")


if __name__ == "__main__":
    main()
