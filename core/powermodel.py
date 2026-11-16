"""Modèles de puissance : profil PPR (modèle CP), estimation SCx (CdA), cibles rapides."""
import math

import numpy as np

G = 9.8067


def fit_cp_model(points) -> dict:
    """Ajuste P(t) = CP + W'/t sur les meilleurs efforts [(secondes, watts)].

    Valide au-delà de ~20 s. Les efforts non maximaux (très en dessous de la
    courbe, ex. échauffement sur le vélo CLM) sont écartés itérativement :
    un seul point sous-maximal effondre la CP. Retourne
    {"cp","wprime","points","excluded"} ou None.
    """
    pts = sorted({(round(float(t)), round(float(p))) for t, p in points
                  if t and p and 20 <= float(t) <= 3600})
    if len(pts) < 2:
        return None
    kept = []
    for t, p in pts:
        if kept and abs(t - kept[-1][0]) <= max(5.0, 0.03 * t):
            if p > kept[-1][1]:
                kept[-1] = (t, p)
        else:
            kept.append((t, p))
    pts = kept
    excluded = []
    cp = wprime = 0.0
    for _ in range(6):
        if len(pts) < 2:
            break
        t = np.array([p[0] for p in pts], dtype=float)
        pw = np.array([p[1] for p in pts], dtype=float)
        x = 1.0 / t
        A = np.vstack([x, np.ones_like(x)]).T
        coef, *_ = np.linalg.lstsq(A, pw, rcond=None)
        wprime, cp = float(coef[0]), float(coef[1])
        if wprime <= 0 or cp <= 50:
            return None
        pred = cp + wprime / t
        below = [p for p, pr in zip(pts, pred) if p[1] < 0.90 * pr]
        if not below or len(pts) - len(below) < 2:
            break
        excluded += below
        below_set = set(below)
        pts = [p for p in pts if p not in below_set]
    if len(pts) < 2 or wprime <= 0 or cp <= 50:
        return None
    t = np.array([p[0] for p in pts], dtype=float)
    pw = np.array([p[1] for p in pts], dtype=float)
    resid = float(np.sqrt(np.mean((pw - (cp + wprime / t)) ** 2)))
    return {"cp": cp, "wprime": wprime, "points": pts, "rmse": resid,
            "excluded": excluded}


def target_power(model: dict, seconds: float) -> float:
    """Puissance cible pour une durée donnée (bornée au domaine de validité)."""
    t = max(20.0, min(3600.0, float(seconds)))
    return max(0.0, model["cp"] + model["wprime"] / t)


def rho_air(temp_c: float, pressure_hpa: float) -> float:
    """Masse volumique de l'air (kg/m³) à partir de la pression de surface."""
    return pressure_hpa * 100.0 / (287.05 * (temp_c + 273.15))


def estimate_scx(power_w: float, speed_kmh: float, gradient_pct: float, mass_kg: float,
                 rho: float, crr: float = 0.004, eta: float = 0.976) -> float:
    """Inverse du modèle de puissance à plat/vent nul : déduit SCx = S·Cx (m², ≈ CdA)."""
    v = speed_kmh / 3.6
    if v < 2.0:
        return float("nan")
    grad = gradient_pct / 100.0
    p_resist = mass_kg * G * v * (grad + crr)
    scx = (power_w * eta - p_resist) / (0.5 * rho * v ** 3)
    return scx


def solve_speed(power_w: float, gradient_pct: float, mass_kg: float, scx: float, rho: float,
                crr: float = 0.004, eta: float = 0.976, headwind_ms: float = 0.0) -> float:
    """Vitesse d'équilibre (m/s) pour une puissance donnée (bissection)."""
    grad = gradient_pct / 100.0

    def f(v):
        v_air = max(0.0, v + headwind_ms)
        return (eta * power_w
                - (0.5 * rho * scx * v_air ** 2 * v + mass_kg * G * (grad + crr) * v))

    lo, hi = 0.2, 32.0
    if f(lo) <= 0:
        return lo
    if f(hi) >= 0:
        return hi
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def quick_target(model: dict, duration_s: float, mass_kg: float, scx: float, rho: float,
                 crr: float = 0.004, eta: float = 0.976, gradient_pct: float = 0.0,
                 headwind_ms: float = 0.0) -> dict:
    """Carte rapide : durée → puissance cible + allure estimée."""
    w = target_power(model, duration_s)
    v = solve_speed(w, gradient_pct, mass_kg, scx, rho, crr, eta, headwind_ms)
    return {
        "watts": round(w),
        "wkg": round(w / mass_kg, 2),
        "speed_kmh": round(v * 3.6, 1),
        "wprime_used_pct": round(100.0 * model["wprime"] / (model["wprime"] + w * duration_s - model["cp"] * duration_s), 1)
        if (w * duration_s - model["cp"] * duration_s) > 0 else 0.0,
    }


def normalized_power(powers: list, times: list) -> float:
    """NP approximée par pondération temporelle des segments (puissance^4)."""
    if not powers:
        return 0.0
    t = np.array(times, dtype=float)
    p = np.array(powers, dtype=float)
    if t.sum() <= 0:
        return 0.0
    return float((np.sum(p ** 4 * t) / t.sum()) ** 0.25)


def wprime_balance(powers: list, times: list, cp: float, wprime: float) -> list:
    """Solde W' simplifié (consommation linéaire, récupération partielle sous CP)."""
    bal, out = wprime, []
    for p, dt in zip(powers, times):
        if p > cp:
            bal -= (p - cp) * dt
        else:
            bal += min(wprime - bal, (cp - p) * dt * 0.75)
        bal = max(0.0, min(wprime, bal))
        out.append(bal)
    return out
