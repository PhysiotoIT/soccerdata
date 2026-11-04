#!/usr/bin/env python3
"""Rozliczenie logu typów — także bez kursów (v2 domyślnie nie używa kursów).

  python3 analityk/kalibracja.py log.csv

Kolumny jak w skillu: data,mecz,rynek,p_model,kurs_wziety,kurs_zamkniecia,stawka,podatek,wynik
(wynik: 1, 0, Z = zwrot; puste = nierozliczony). Kursy i stawka mogą być puste: wtedy liczą się
tylko trafność modelu (Brier, log loss, kalibracja w przedziałach P). ROI i CLV — tylko dla
wierszy z kursem i stawką.
"""

from __future__ import annotations

import csv
import math
import sys


def _liczba(x: str | None) -> float | None:
    x = (x or "").strip().replace(",", ".")
    return float(x) if x else None


def rozlicz(wiersze: list[dict]) -> dict:
    n = 0
    brier = logloss = 0.0
    kosze: dict[int, list[float]] = {}
    stawki = zysk = 0.0
    clv = []
    for r in wiersze:
        w = (r.get("wynik") or "").strip().upper()
        p = _liczba(r.get("p_model"))
        kurs, zamk = _liczba(r.get("kurs_wziety")), _liczba(r.get("kurs_zamkniecia"))
        stawka, podatek = _liczba(r.get("stawka")), _liczba(r.get("podatek")) or 0.88
        if kurs and zamk:
            clv.append(kurs / zamk - 1)
        if not w or p is None:
            continue
        if kurs and stawka:
            stawki += stawka
            if w == "Z":
                zysk += stawka * (podatek - 1)
            else:
                zysk += stawka * (podatek * kurs - 1) if w == "1" else -stawka
        if w == "Z":
            continue
        y = 1.0 if w == "1" else 0.0
        n += 1
        brier += (p - y) ** 2
        pc = min(max(p, 1e-6), 1 - 1e-6)
        logloss += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
        k = kosze.setdefault(min(int(p * 10), 9), [0, 0.0, 0.0])
        k[0], k[1], k[2] = k[0] + 1, k[1] + p, k[2] + y
    return {
        "n": n,
        "brier": brier / n if n else None,
        "logloss": logloss / n if n else None,
        "kosze": {b: (int(k[0]), k[1] / k[0], k[2] / k[0]) for b, k in sorted(kosze.items())},
        "roi": zysk / stawki if stawki else None,
        "zysk": zysk,
        "clv": sum(clv) / len(clv) if clv else None,
        "n_clv": len(clv),
    }


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit(__doc__)
    with open(argv[0], encoding="utf-8") as fh:
        w = rozlicz(list(csv.DictReader(fh)))
    if not w["n"]:
        print("Brak rozliczonych typów.")
        return
    print(f"Rozliczone typy: {w['n']} | Brier {w['brier']:.4f} | log loss {w['logloss']:.4f}")
    print("(Brier: 0.25 = rzut monetą przy P 50%; im niżej, tym lepiej)")
    for b, (n, p, traf) in w["kosze"].items():
        print(f"P {b * 10}-{b * 10 + 10}%: n={n:>3}  model {p:.0%}  trafione {traf:.0%}")
    if w["roi"] is not None:
        print(f"ROI (typy z kursem i stawką): {w['roi']:+.1%} (wynik {w['zysk']:+.2f} zł)")
    if w["clv"] is not None:
        print(f"CLV: średnio {w['clv']:+.2%} z {w['n_clv']} typów")
    if w["n"] < 50:
        print("Uwaga: poniżej ~50 typów kalibracja w przedziałach to głównie szum.")


if __name__ == "__main__":
    main()
