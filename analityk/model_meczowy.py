#!/usr/bin/env python3
"""Model meczowy: Dixon-Coles + rynki pochodne + statystyki (NB) + EV po polskim podatku.
(Kopia skryptu ze skilla analityk-meczowy; v2 dodaje klucz "rynki": rynki do tabeli bez kursów.)
  python3 model_meczowy.py mecz.json            analiza meczu
  python3 model_meczowy.py --log log.csv        rozliczenie: ROI, CLV, Brier, kalibracja
  python3 model_meczowy.py --normal MU SIGMA L  szybki rozkład normalny (np. NBA: suma, handicap)"""
import csv, json, math, re, sys, unicodedata
from statistics import NormalDist

NG = 11  # gole 0..10
UWAGI = set()

def pois(lam, n):
    if lam <= 0:
        return [1.0] + [0.0] * (n - 1)
    return [math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1)) for k in range(n)]

def negbin(m, vr, n):
    """Ujemny dwumianowy: średnia m, wariancja vr*m (vr <= 1 -> Poisson)."""
    if vr <= 1.0001 or m <= 0:
        return pois(m, n)
    r = m / (vr - 1)
    p = r / (r + m)
    return [math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                     + r * math.log(p) + k * math.log(1 - p)) for k in range(n)]

def zasieg(m, vr):
    return int(m + 8 * math.sqrt(max(m * vr, 1.0)) + 10)

def macierz(lh, la, rho):
    ph, pa = pois(lh, NG), pois(la, NG)
    M = [[ph[i] * pa[j] for j in range(NG)] for i in range(NG)]
    for (i, j), tau in {(0, 0): 1 - lh * la * rho, (0, 1): 1 + lh * rho,
                        (1, 0): 1 + la * rho, (1, 1): 1 - rho}.items():
        M[i][j] *= max(tau, 0.0)
    s = sum(map(sum, M))
    return [[x / s for x in w] for w in M]

def splot(a, b):
    out = [0.0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        if x > 1e-15:
            for j, y in enumerate(b):
                out[i + j] += x * y
    return out

def norm(s):
    s = unicodedata.normalize("NFKD", str(s).translate(str.maketrans("łŁ", "lL")))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.strip().lower())

def stan(lh, la, cfg, skala=1.0):
    M = macierz(lh, la, cfg.get("rho", -0.10))
    tot, roz = [0.0] * (2 * NG - 1), {}
    for i in range(NG):
        for j in range(NG):
            tot[i + j] += M[i][j]
            roz[i - j] = roz.get(i - j, 0.0) + M[i][j]
    u1 = cfg.get("udzial_1pol", 0.45)
    d = {"": tot, "gosp.": [sum(w) for w in M], "gosc": [sum(M[i][j] for i in range(NG)) for j in range(NG)],
         "1. pol.": pois(u1 * (lh + la), NG), "2. pol.": pois((1 - u1) * (lh + la), NG)}
    for nazwa, s in cfg.get("statystyki", {}).items():
        k, vr = norm(nazwa), s.get("var_ratio", 1.15)
        if "suma" in s:
            m = s["suma"] * skala
            d[k] = negbin(m, vr, zasieg(m, vr))
        else:
            a, b = s["gosp"] * skala, s["gosc"] * skala
            d[k + " 1"], d[k + " 2"] = negbin(a, vr, zasieg(a, vr)), negbin(b, vr, zasieg(b, vr))
            d[k] = splot(d[k + " 1"], d[k + " 2"])
    for z in cfg.get("zawodnicy", []):
        sc = z["minuty"] if isinstance(z["minuty"], list) else [[1.0, z["minuty"]]]
        wsum, vr = sum(w for w, _ in sc), z.get("var_ratio", 1.0)
        rate = z["na90"] * z.get("korekta", 1.0) * skala / 90
        n = zasieg(rate * max(mn for _, mn in sc), vr)
        dist = [0.0] * n
        for w, mn in sc:
            for k, x in enumerate(negbin(rate * mn, vr, n)):
                dist[k] += w / wsum * x
        d[norm(z["nazwa"])] = dist
    return {"M": M, "d": d, "roz": roz}

def azjat(pary, f, linia):
    """Zakład z możliwym zwrotem; linie .25/.75 dzielone na pół stawki."""
    pary = list(pary)
    linie = [linia] if round(linia * 4) % 2 == 0 else [linia - 0.25, linia + 0.25]
    out = []
    for l in linie:
        pw = pp = pl = 0.0
        for v, x in pary:
            r = f(v, l)
            if r > 1e-9:
                pw += x
            elif r < -1e-9:
                pl += x
            else:
                pp += x
        out.append((1 / len(linie), pw, pp, pl))
    return out

PRED = {"1": lambda i, j: i > j, "x": lambda i, j: i == j, "2": lambda i, j: i < j,
        "1x": lambda i, j: i >= j, "x2": lambda i, j: i <= j, "12": lambda i, j: i != j,
        "btts tak": lambda i, j: i > 0 and j > 0, "btts nie": lambda i, j: i == 0 or j == 0,
        "1 do zera": lambda i, j: i > 0 and j == 0, "2 do zera": lambda i, j: j > 0 and i == 0,
        "czyste konto 1": lambda i, j: j == 0, "czyste konto 2": lambda i, j: i == 0}

def pred_gole(k):
    if k in PRED:
        return PRED[k]
    m = re.fullmatch(r"wynik (\d+) ?[:-] ?(\d+)", k)
    if m:
        a, b = int(m[1]), int(m[2])
        return lambda i, j: i == a and j == b
    m = re.fullmatch(r"(gosp\. |gosc )?(over|under) (\d+\.5)", k)
    if m:
        lin, val = float(m[3]), {"": lambda i, j: i + j, "gosp.": lambda i, j: i,
                                 "gosc": lambda i, j: j}[(m[1] or "").strip()]
        return (lambda i, j: val(i, j) > lin) if m[2] == "over" else (lambda i, j: val(i, j) < lin)
    return None

def czesci(st, klucz):
    """Rynek -> [(waga, P wygranej, P zwrotu, P przegranej)]."""
    k = norm(klucz)
    if "&" in k:  # kombinacja z jednego meczu (bet builder)
        p, preds = 1.0, []
        for leg in (x.strip() for x in k.split("&")):
            f = pred_gole(leg)
            if f:
                preds.append(f)
                continue
            pw, pp, _, _ = podsum(czesci(st, leg))
            if pp > 1e-9:
                raise ValueError(f"noga ze zwrotem nie pasuje do kombinacji: {leg}")
            p *= pw
            UWAGI.add(f"'{leg}' w kombinacji liczona jako niezależna od goli (przybliżenie)")
        if preds:
            p *= sum(st["M"][i][j] for i in range(NG) for j in range(NG) if all(f(i, j) for f in preds))
        return [(1.0, p, 0.0, 1 - p)]
    f = pred_gole(k)
    if f:
        p = sum(st["M"][i][j] for i in range(NG) for j in range(NG) if f(i, j))
        return [(1.0, p, 0.0, 1 - p)]
    m = re.fullmatch(r"(ah|dnb) ([12])(?: ([+-]?\d+(?:\.\d+)?))?", k)
    if m:
        lin = 0.0 if m[1] == "dnb" else float(m[3] or 0)
        g = (lambda v, l: v + l) if m[2] == "1" else (lambda v, l: l - v)
        return azjat(st["roz"].items(), g, lin)
    m = re.fullmatch(r"(.*?) ?(over|under) (\d+(?:\.\d+)?)", k)
    if m:
        pref = m[1].strip()
        if pref not in st["d"]:
            raise ValueError(f"brak rozkładu '{pref}' (dodaj go w 'statystyki' lub 'zawodnicy')")
        g = (lambda v, l: v - l) if m[2] == "over" else (lambda v, l: l - v)
        return azjat(enumerate(st["d"][pref]), g, float(m[3]))
    if k == "gol w obu polowach":
        p = (1 - st["d"]["1. pol."][0]) * (1 - st["d"]["2. pol."][0])
        return [(1.0, p, 0.0, 1 - p)]
    raise ValueError(f"nieznany rynek: {klucz}")

def podsum(cz):
    pw = sum(w * a for w, a, _, _ in cz)
    pp = sum(w * b for w, _, b, _ in cz)
    pl = sum(w * c for w, _, _, c in cz)
    return pw, pp, pl, (pw / (pw + pl) if pw + pl > 1e-12 else 0.0)

def ev(cz, kurs, t):
    """EV na 1 zł wpłaty; wygrana = t*kurs, zwrot = t (podatek od stawki nie wraca)."""
    return sum(w * (a * t * kurs + b * t) for w, a, b, _ in cz) - 1

def kurs_min(cz, t, prog=0.0):
    pw, pp, _, _ = podsum(cz)
    return (1 + prog - t * pp) / (t * pw) if pw > 1e-12 else float("inf")

def devig(kursy):
    """Usunięcie marży metodą potęgową."""
    inv = [1 / k for k in kursy]
    lo, hi = 0.05, 20.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if sum(x ** mid for x in inv) > 1 else (lo, mid)
    p = [x ** ((lo + hi) / 2) for x in inv]
    return [x / sum(p) for x in p]

def lambdy_z_rynku(k1x2, kou, rho):
    p1, px, p2 = devig(k1x2)
    po = devig(kou)[0] if kou else None

    def blad(lh, la):
        M = macierz(lh, la, rho)
        h = sum(M[i][j] for i in range(NG) for j in range(i))
        d = sum(M[i][i] for i in range(NG))
        e = (h - p1) ** 2 + (d - px) ** 2 + (1 - h - d - p2) ** 2
        if po is not None:
            e += (1 - (M[0][0] + M[1][0] + M[0][1] + M[2][0] + M[1][1] + M[0][2]) - po) ** 2
        return e

    siatka = [0.2 + 0.05 * k for k in range(77)]
    _, a0, b0 = min((blad(a, b), a, b) for a in siatka for b in siatka)
    kroki = [0.005 * k for k in range(-10, 11)]
    _, a1, b1 = min((blad(a0 + x, b0 + y), a0 + x, b0 + y) for x in kroki for y in kroki
                    if a0 + x > 0.05 and b0 + y > 0.05)
    return a1, b1

def main(cfg):
    rho, t, prog = cfg.get("rho", -0.10), cfg.get("podatek", 0.88), cfg.get("prog", 0.05)
    lm, rk = cfg.get("model"), cfg.get("rynek") or {}
    lr = lambdy_z_rynku(rk["1x2"], rk.get("ou25"), rho) if rk.get("1x2") else None
    if lm and lr:
        w = cfg.get("waga_modelu", 0.5)
        lh, la = w * lm["lh"] + (1 - w) * lr[0], w * lm["la"] + (1 - w) * lr[1]
    elif lm or lr:
        lh, la = (lm["lh"], lm["la"]) if lm else lr
    else:
        sys.exit("Podaj 'model': {lh, la} i/lub 'rynek': {1x2: [k1, kX, k2], ou25: [over, under]}.")
    print(f"=== {cfg.get('mecz', 'Mecz')} ===")
    if lm:
        print(f"λ model:   {lm['lh']:.2f} – {lm['la']:.2f}")
    if lr:
        delta = f"   Δ model-rynek: {lm['lh'] - lr[0]:+.2f} / {lm['la'] - lr[1]:+.2f}" if lm else ""
        print(f"λ rynek:   {lr[0]:.2f} – {lr[1]:.2f}{delta}")
        if lm and max(abs(lm["lh"] - lr[0]), abs(lm["la"] - lr[1])) > 0.25:
            print("! Duża różnica model-rynek: najpierw szukaj brakującej informacji (składy, dane), potem value.")
    print(f"λ końcowe: {lh:.2f} – {la:.2f} (suma {lh + la:.2f}) | rho {rho} | podatek {t} | próg EV {prog:.0%}")
    dl = cfg.get("czulosc", 0.15)
    baza = stan(lh, la, cfg)
    scen = [stan(max(lh + a, 0.05), max(la + b, 0.05), cfg, s)
            for a, b, s in ((dl, dl, 1.1), (dl, -dl, 1.0), (-dl, dl, 1.0), (-dl, -dl, 0.9))]
    M = baza["M"]
    top = sorted(((M[i][j], f"{i}:{j}") for i in range(NG) for j in range(NG)), reverse=True)[:6]
    print("Wyniki dokładne: " + " | ".join(f"{s} {p:.1%}" for p, s in top))

    rynki = ["1", "X", "2", "1X", "X2", "12", "DNB 1", "DNB 2", "Over 1.5", "Under 1.5", "Over 2.5",
             "Under 2.5", "Over 3.5", "Under 3.5", "BTTS tak", "BTTS nie", "Gosp. Over 0.5", "Gosp. Over 1.5",
             "Gość Over 0.5", "Gość Over 1.5", "1 do zera", "2 do zera", "1. poł. Over 0.5",
             "1. poł. Over 1.5", "Gol w obu połowach", "AH 1 -1.5", "AH 1 -1", "AH 1 +1", "AH 1 +1.5",
             "AH 2 -1.5", "AH 2 -1", "AH 2 +1", "AH 2 +1.5"]
    for nazwa, s in cfg.get("statystyki", {}).items():
        c = round(s.get("suma", s.get("gosp", 0) + s.get("gosc", 0)))
        rynki += [f"{nazwa} {o} {L:g}" for L in (c - 1.5, c - 0.5, c + 0.5, c + 1.5) if L > 0
                  for o in ("Over", "Under")]
        if "gosp" in s:
            rynki += [f"{nazwa} {st} Over {L:g}" for st, mm in (("1", s["gosp"]), ("2", s["gosc"]))
                      for L in (round(mm) - 0.5, round(mm) + 0.5) if L > 0]
    for z in cfg.get("zawodnicy", []):
        rynki += [f"{z['nazwa']} Over {L:g}" for L in z.get("linie", [0.5, 1.5])]
    kursy = cfg.get("kursy", {})
    # v2: "rynki" = dodatkowe rynki do tabeli P bez kursów (np. "Wynik 2:1", "1 & Over 1.5")
    rynki += list(cfg.get("rynki", [])) + list(kursy)
    widziane = set()
    rynki = [r for r in rynki if not (norm(r) in widziane or widziane.add(norm(r)))]

    wiersze = []
    for r in rynki:
        try:
            cz, czs = czesci(baza, r), [czesci(s, r) for s in scen]
        except ValueError as e:
            print(f"! {e}")
            continue
        pe = podsum(cz)[3]
        zakres = [podsum(c)[3] for c in czs] + [pe]
        wiersze.append((r, cz, czs, pe, min(zakres), max(zakres)))
    wiersze.sort(key=lambda w: -w[3])
    print(f"\n{'Rynek':<36}{'P':>7}{'zakres':>11}{'zwrot':>7}{'fair':>7}{'min':>7}")
    for r, cz, czs, pe, lo, hi in wiersze:
        pp = podsum(cz)[1]
        print(f"{r:<36}{pe:>7.1%}{f'{lo:.0%}-{hi:.0%}':>11}{(f'{pp:.0%}' if pp > 0.005 else '-'):>7}"
              f"{kurs_min(cz, 1.0):>7.2f}{kurs_min(cz, t, prog):>7.2f}")
    print(f"(P bez zwrotów; fair = kurs bez marży i podatku; min = kurs dający EV >= {prog:.0%} po podatku)")

    if kursy:
        bank, kf, cap = cfg.get("bankroll", 0), cfg.get("kelly", 0.25), cfg.get("max_stawka", 0.02)
        idx, oceny = {w[0]: w for w in wiersze}, []
        for r, k in kursy.items():
            if r not in idx:
                continue
            _, cz, czs, pe, _, _ = idx[r]
            e, e0 = ev(cz, k, t), ev(cz, k, 1.0)
            emin = min(ev(c, k, t) for c in czs + [cz])
            b = t * k - 1
            f = min(max(e, 0) / b * kf, cap) if b > 0 else 0.0
            if e >= prog:
                ocena = "VALUE odporne" if emin > 0 else "VALUE (kruche)"
            elif e0 >= cfg.get("prog_ako", 0.03):
                ocena = "tylko noga AKO"
            else:
                ocena = "za mało" if e > 0 else "nie grać"
            oceny.append((e, r, k, pe, e0, emin, f, ocena))
        print(f"\n=== VALUE (EV po podatku: wypłata = kurs × {t}; EV0 = przed podatkiem) ===")
        print(f"{'Rynek':<36}{'kurs':>6}{'P':>7}{'EV':>8}{'EV0':>8}{'EV_min':>8}{'stawka':>9}  ocena")
        for e, r, k, pe, e0, emin, f, ocena in sorted(oceny, reverse=True):
            stw = f"{f * bank:.0f} zł" if bank else f"{f:.1%}"
            print(f"{r:<36}{k:>6.2f}{pe:>7.1%}{e:>+8.1%}{e0:>+8.1%}{emin:>+8.1%}{stw:>9}  {ocena}")

    ako = cfg.get("ako")
    if ako:
        nogi = ako["nogi"]
        P = math.prod(p for p, _ in nogi)
        K = math.prod(k for _, k in nogi) * ako.get("boost", 1.0)
        ta, stw = ako.get("podatek", t), ako.get("stawka", 10.0)
        wyp = stw * ta * K
        if wyp > cfg.get("prog_wygranej", 2280):
            wyp *= 1 - cfg.get("podatek_wygranej", 0.10)
        print(f"\n=== AKO, nóg: {len(nogi)} === P {P:.1%} | kurs {K:.2f} | wypłata {wyp:.2f} zł za {stw:.2f} zł"
              f" | EV {P * wyp / stw - 1:+.1%} (przed podatkami {P * K - 1:+.1%})")
        for i, (p, k) in enumerate(nogi, 1):
            print(f"  noga {i}: P {p:.1%} × kurs {k:.2f} -> wartość {p * k - 1:+.1%} (przed podatkiem)")
        print(f"  Średnia wartość nogi potrzebna do EV >= {prog:.0%}: "
              f"{((1 + prog) / ta) ** (1 / len(nogi)) - 1:+.1%} (przed podatkiem)")
    for u in sorted(UWAGI):
        print(f"Uwaga: {u}")

def kalibracja(plik, t_def=0.88):
    n = nb = 0
    brier = zysk = stawki = 0.0
    clv, kosz = [], {}
    with open(plik, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            p, k = float(r["p_model"]), float(r["kurs_wziety"])
            if (r.get("kurs_zamkniecia") or "").strip():
                clv.append(k / float(r["kurs_zamkniecia"]) - 1)
            w = (r.get("wynik") or "").strip().upper()
            if not w:
                continue
            s, t = float(r.get("stawka") or 1), float(r.get("podatek") or t_def)
            n += 1
            stawki += s
            if w == "Z":
                zysk += s * (t - 1)
                continue
            y = 1.0 if w == "1" else 0.0
            zysk += s * (t * k - 1) if y else -s
            brier += (p - y) ** 2
            nb += 1
            c = kosz.setdefault(min(int(p * 10), 9), [0, 0.0, 0.0])
            c[0], c[1], c[2] = c[0] + 1, c[1] + p, c[2] + y
    print(f"Rozliczone: {n} | ROI {zysk / stawki:+.1%} (wynik {zysk:+.2f})" if stawki else "Brak rozliczonych typów")
    if nb:
        print(f"Brier: {brier / nb:.4f} (n={nb})")
    if clv:
        dodatnie = sum(1 for x in clv if x > 0) / len(clv)
        print(f"CLV: średnio {sum(clv) / len(clv):+.2%}, dodatni w {dodatnie:.0%} z {len(clv)} typów")
    for b in sorted(kosz):
        c = kosz[b]
        print(f"P {b * 10}-{b * 10 + 10}%: n={c[0]:>3}  model {c[1] / c[0]:.0%}  trafione {c[2] / c[0]:.0%}")

def normal_cli(mu, sigma, linia):
    nd = NormalDist(mu, sigma)
    if linia % 1:
        pu = nd.cdf(linia)
        print(f"P(> {linia:g}) = {1 - pu:.1%} | P(< {linia:g}) = {pu:.1%}")
    else:
        pu, pz = nd.cdf(linia - 0.5), nd.cdf(linia + 0.5) - nd.cdf(linia - 0.5)
        print(f"P(> {linia:g}) = {1 - pu - pz:.1%} | zwrot {pz:.1%} | P(< {linia:g}) = {pu:.1%}")

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "--log":
        kalibracja(a[1])
    elif a[0] == "--normal":
        normal_cli(*map(float, a[1:4]))
    else:
        with open(a[0], encoding="utf-8") as fh:
            main(json.load(fh))
