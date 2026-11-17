"""Modèle TTT : drafting scientifique, vitesse de groupe maintenue, rotations,
analyse post-course d'un fichier coureur.

Facteurs de traînée (fraction du SCx solo conservée selon la position dans la
ligne), d'après la littérature :
- Blocken et al. (2018, TU Eindhoven) — aérodynamique du contre-la-montre par
  équipe, positions en ligne, écart roue-roue ;
- Barry et al. (2016) — réduction de traînée derrière 1 à n coureurs ;
- Kyle & Burke (1979) — drafting roue dans la roue (~-30 à -45 %).
"""
import numpy as np
import pandas as pd

from .powermodel import solve_speed, target_power
from .course import headwind_ms

G = 9.8067

DRAFT_GAPS = {
    "Roue dans la roue (~0,3 m)": [1.00, 0.62, 0.55, 0.52, 0.51, 0.50, 0.50, 0.50, 0.50],
    "Mi-roue (~0,5 m)": [1.00, 0.70, 0.62, 0.58, 0.56, 0.55, 0.55, 0.55, 0.55],
    "Une roue (~1 m)": [1.00, 0.78, 0.70, 0.66, 0.64, 0.63, 0.63, 0.63, 0.63],
}
DEFAULT_GAP = "Mi-roue (~0,5 m)"


def draft_factor(gap: str, position: int) -> float:
    """Fraction de SCx conservée à la `position` (1 = devant)."""
    table = DRAFT_GAPS.get(gap, DRAFT_GAPS[DEFAULT_GAP])
    return table[min(max(position - 1, 0), len(table) - 1)]


def required_power(v: float, gradient_pct: float, mass_kg: float, scx: float,
                   rho: float, crr: float = 0.004, eta: float = 0.976,
                   headwind: float = 0.0) -> float:
    """Puissance (W) nécessaire pour tenir v (m/s) — inverse exact de solve_speed."""
    grad = gradient_pct / 100.0
    v_air = max(0.0, v + headwind)
    return (0.5 * rho * scx * v_air ** 2 * v + mass_kg * G * (grad + crr) * v) / eta


def _v_for(p: float, g: float, mass: float, scx: float, rho: float,
           crr: float, eta: float, w: float) -> float:
    return solve_speed(max(p, 1.0), g, mass, scx, rho, crr, eta, w)


def team_simulation(segs: pd.DataFrame, riders: list, pull_s: float = 25.0,
                    gap: str = DEFAULT_GAP, rho: float = 1.225, crr: float = 0.004,
                    eta: float = 0.976, wind_ms: float = 0.0, wind_dir: float = 0.0,
                    order: list = None, iterations: int = 3) -> dict:
    """Simule le TTT avec rotations : vitesse maintenue segment par segment.

    La vitesse du groupe est fixée à chaque instant par le coureur de tête
    (il tient la puissance nécessaire) ET par le contrainte d'abri : chaque
    coureur derrière ne dépasse pas sa puissance solo / facteur d'abri.
    riders : [{name, cp, wprime, weight, scx}] ; order : indices dans l'ordre
    de rotation. Retourne {total_s, avg_kmh, per_rider, pulls, order_used}.
    """
    n = len(riders)
    if n == 0:
        return None
    order = list(order) if order else list(range(n))
    grads = segs["gradient_pct"].values
    bears = segs["bearing_deg"].values
    dists = segs["dist_m"].values
    winds = np.array([headwind_ms(wind_ms, wind_dir, b) for b in bears])
    cum = np.cumsum(dists)
    total_d = float(dists.sum())
    masses = [r["weight"] + 8.0 for r in riders]
    scxs = [r.get("scx", 0.21) for r in riders]

    T = total_d / 12.0
    p_solo = np.array([r["cp"] for r in riders], dtype=float)
    v_solo = None
    for _ in range(iterations):
        p_solo = np.array([target_power({"cp": r["cp"], "wprime": r.get("wprime", 20000.0)}, T)
                           for r in riders])
        v_solo = np.zeros((n, len(grads)))
        for i in range(n):
            for j in range(len(grads)):
                v_solo[i][j] = _v_for(p_solo[i], grads[j], masses[i], scxs[i],
                                      rho, crr, eta, winds[j])
        T = float((dists / np.maximum(v_solo.min(axis=0), 0.3)).sum())

    def seg_index(d):
        return int(np.searchsorted(cum, d, side="left"))

    t = 0.0
    dist = 0.0
    dt = 1.0
    per = [{"name": riders[i]["name"], "front_s": 0.0, "front_ws": 0.0,
            "draft_s": 0.0, "draft_ws": 0.0, "pulls": 0, "in_front": False}
           for i in range(n)]
    pulls = []
    cur_front = None
    pull_start = 0.0
    guard = 0
    while dist < total_d and guard < 200000:
        guard += 1
        j = min(seg_index(dist), len(grads) - 1)
        k = int(t // pull_s) % n
        front = order[k]
        if front != cur_front:
            if cur_front is not None:
                pulls.append({"rider": cur_front, "t0": pull_start, "t1": t})
            cur_front = front
            pull_start = t
            per[front]["pulls"] += 1
        v = 1e9
        for m in range(n):
            i = order[(k + m) % n]
            f = draft_factor(gap, m + 1)
            v_i = _v_for(p_solo[i] / f, grads[j], masses[i], scxs[i],
                         rho, crr, eta, winds[j])
            v = min(v, v_i)
        v = max(v, 0.5)
        step = min(dt, (total_d - dist) / v)
        p_req = required_power(v, grads[j], masses[front], scxs[front],
                               rho, crr, eta, winds[j])
        per[front]["front_s"] += step
        per[front]["front_ws"] += p_req * step
        for m in range(1, n):
            i = order[(k + m) % n]
            f = draft_factor(gap, m + 1)
            per[i]["draft_s"] += step
            per[i]["draft_ws"] += p_req * f * step
        dist += v * step
        t += step
    if cur_front is not None:
        pulls.append({"rider": cur_front, "t0": pull_start, "t1": t})

    for p in per:
        p["avg_front_w"] = round(p["front_ws"] / p["front_s"]) if p["front_s"] else 0
        p["avg_draft_w"] = round(p["draft_ws"] / p["draft_s"]) if p["draft_s"] else 0
        p["pct_front"] = round(100.0 * p["front_s"] / t, 1) if t else 0.0
        p["avg_race_w"] = round((p["front_ws"] + p["draft_ws"]) / t) if t else 0
    return {"total_s": t, "avg_kmh": round(total_d / max(t, 1) * 3.6, 1),
            "per_rider": per, "pulls": pulls, "order_used": order,
            "p_solo": [round(float(p)) for p in p_solo]}


def best_order(segs: pd.DataFrame, riders: list, pull_s: float, gap: str,
               rho: float, crr: float = 0.004, eta: float = 0.976,
               wind_ms: float = 0.0, wind_dir: float = 0.0,
               max_perms: int = 720) -> dict:
    """Cherche le meilleur ordre de rotation (force brute jusqu'à 6 coureurs,
    sinon heuristiques). Retourne {order, total_s, candidates}."""
    import itertools
    n = len(riders)
    if n == 0:
        return None
    base = team_simulation(segs, riders, pull_s, gap, rho, crr, eta,
                           wind_ms, wind_dir, order=list(range(n)), iterations=2)
    if base is None:
        return None

    def time_of(order):
        return team_simulation(segs, riders, pull_s, gap, rho, crr, eta,
                               wind_ms, wind_dir, order=order, iterations=1)["total_s"]

    base_total_s = time_of(list(range(n)))

    if n <= 6:
        perms = list(itertools.permutations(range(n)))[:max_perms]
    else:
        by_solo = sorted(range(n), key=lambda i: -base["p_solo"][i])
        perms = [tuple(range(n)), tuple(by_solo), tuple(reversed(by_solo))]
    results = [(time_of(list(o)), list(o)) for o in perms]
    results.sort(key=lambda x: x[0])
    best_t, best_o = results[0]
    cands = [{"order": o, "total_s": tt} for tt, o in results[:3]]
    return {"order": best_o, "total_s": best_t, "candidates": cands,
            "base_total_s": base_total_s}


def analyze_ttt_file(records: list, mass_kg: float, scx: float, rho: float,
                     crr: float = 0.004, eta: float = 0.976,
                     wind_ms: float = 0.0, wind_dir: float = 0.0) -> dict:
    """Analyse post-course : position estimée à chaque instant d'après le
    rapport puissance réelle / puissance solo attendue à la même vitesse.

    records : [{time, dist, power, speed, ele}] (parse_fit_activity).
    Retourne {df, pct_pos, stints, conclusions}.
    """
    rows = [r for r in records
            if r.get("power") and r.get("dist") is not None]
    if len(rows) < 20:
        return None
    d = pd.DataFrame(rows)
    d["dist"] = d["dist"].astype(float)
    d["power"] = d["power"].astype(float)
    if "ele" not in d.columns:
        d["ele"] = np.nan
    if d["ele"].notna().sum() > 10:
        d["ele"] = d["ele"].astype(float)
        d["grad"] = (d["ele"].diff(5) / (d["dist"].diff(5) + 1.0) * 100.0).clip(-18, 22).fillna(0.0)
    else:
        d["grad"] = 0.0
    if "speed" in d.columns and d["speed"].notna().sum() > 10:
        d["v"] = d["speed"].astype(float)
    else:
        d["v"] = (d["dist"].diff().fillna(0.0)).clip(lower=0.0)
    d["bearing"] = 0.0
    if "lat" not in d.columns:
        d["lat"] = np.nan
    if "lon" not in d.columns:
        d["lon"] = np.nan
    if d["lat"].notna().sum() > 10:
        from .course import bearing as _bearing
        brs = [0.0]
        lat, lon = d["lat"].values, d["lon"].values
        for i in range(1, len(d)):
            if (np.isfinite(lat[i]) and np.isfinite(lon[i])
                    and np.isfinite(lat[i - 1]) and np.isfinite(lon[i - 1])):
                brs.append(_bearing(float(lat[i - 1]), float(lon[i - 1]),
                                    float(lat[i]), float(lon[i])))
            else:
                brs.append(brs[-1])
        d["bearing"] = brs
    d["wind"] = [headwind_ms(wind_ms, wind_dir, b) for b in d["bearing"]]

    p_exp = []
    gap_tbl = DRAFT_GAPS[DEFAULT_GAP]
    for v, g, w in zip(d["v"].values, d["grad"].values, d["wind"].values):
        if v < 2.0:
            p_exp.append(np.nan)
            continue
        p_exp.append(required_power(v, g, mass_kg, scx, rho, crr, eta, w))
    d["p_exp"] = p_exp
    d["ratio"] = d["power"] / d["p_exp"]
    d.loc[d["v"] < 2.0, "ratio"] = np.nan
    positions = []
    for r in d["ratio"].values:
        if np.isnan(r):
            positions.append(0)
            continue
        best, bestpos = 9e9, 0
        for pos, f in enumerate(gap_tbl[:6], start=1):
            if abs(r - f) < best:
                best, bestpos = abs(r - f), pos
        positions.append(bestpos)
    d["position"] = positions

    total = int((d["position"] > 0).sum())
    pct_pos = {}
    for pos in range(1, 7):
        cnt = int((d["position"] == pos).sum())
        pct_pos[pos] = round(100.0 * cnt / max(total, 1), 1)

    stints = []
    cur = None
    for i, pos in enumerate(d["position"].values):
        if pos == 1:
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        else:
            if cur is not None:
                stints.append(cur)
                cur = None
    if cur is not None:
        stints.append(cur)
    # fusionne les stints séparés de < 3 s (micro-coupures)
    merged = []
    for s in stints:
        if merged and s[0] - merged[-1][1] <= 3:
            merged[-1][1] = s[1]
        else:
            merged.append(list(s))
    front_pct = round(100.0 * sum(s[1] - s[0] + 1 for s in merged) / max(total, 1), 1)
    avg_ratio_back = float(d.loc[d["position"].between(2, 4), "ratio"].mean() or 0.0)
    avg_front_w = float(d.loc[d["position"] == 1, "power"].mean() or 0.0)
    avg_back_w = float(d.loc[d["position"].between(2, 4), "power"].mean() or 0.0)

    conclusions = []
    if total:
        conclusions.append(f"Temps devant : **{front_pct} %** "
                           f"({len(merged)} relais, durée moyenne "
                           f"{(sum(s[1] - s[0] + 1 for s in merged) / max(len(merged), 1)):.0f} s).")
    if avg_ratio_back:
        if avg_ratio_back > 0.80:
            conclusions.append("⚠️ **Pas assez abrité derrière** (rapport moyen "
                               f"{avg_ratio_back:.2f}) : celui qui est devant ne le protège pas "
                               "(écart trop grand) ou roule trop vite pour lui — "
                               "changez le coureur de tête ou resserrez la ligne.")
        elif avg_ratio_back < 0.55:
            conclusions.append(f"✅ Très bien abrité derrière (rapport moyen {avg_ratio_back:.2f}).")
        else:
            conclusions.append(f"Abri correct derrière (rapport moyen {avg_ratio_back:.2f}).")
    if avg_front_w:
        conclusions.append(f"Puissance moyenne devant : **{avg_front_w:.0f} W** ; "
                           f"à l'abri : **{avg_back_w:.0f} W**.")
    return {"df": d, "pct_pos": pct_pos, "stints": merged,
            "front_pct": front_pct, "avg_ratio_back": avg_ratio_back,
            "avg_front_w": avg_front_w, "avg_back_w": avg_back_w,
            "conclusions": conclusions}
