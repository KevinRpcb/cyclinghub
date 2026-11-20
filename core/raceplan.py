"""Plan de course : montées principales, temps par rôle, délai (fenêtre de
contrôle), énergie (kJ) et glucides (g/h).

Modèle volontairement simple et transparent :
- montées détectées sur les segments (pente moyenne et longueur minimales) ;
- leader = effort max type « record » (modèle CP : P = CP + W′/t, point fixe
  avec l'équilibre physique de la pente) ;
- coéquipiers / sprinters = puissance bornée en % de CP sur les montées et
  sur le roulant (gestion de journée pour rester dans les délais) ;
- délai = temps gagnant × (1 + %) — le % est rempli par le coach, il change
  à chaque course ;
- glucides g/h : consensus récent (Burke 2011 ; Jeukendrup 2014 ; actes
  « carbohydrate periodization ») selon durée et intensité, ajusté par
  scénario de course rapide/lente.
"""
import numpy as np

from core.powermodel import solve_speed

ROLES = ["Leader", "Coéquipier", "Sprinter"]

CLIMB_TRIG = 2.0        # % : au-dessus, on accumule la montée
CLIMB_SOFT = 1.0        # % : faux plat toléré dans une montée
CLIMB_MIN_GRAD = 3.0    # % moyen minimum
CLIMB_MIN_LEN = 800.0   # m
CLIMB_MIN_DPLUS = 25.0  # m

FLAT_PCT = {"Leader": 0.68, "Coéquipier": 0.55, "Sprinter": 0.50}
CLIMB_PCT = {"Coéquipier": 0.85, "Sprinter": 0.72}
SCEN_FLAT = {"Rapide": 1.15, "Normal": 1.0, "Lent": 0.85}


def _push(climbs, cur, min_grad, min_len_m, min_dplus):
    if cur["dist"] < min_len_m or cur["up"] < min_dplus:
        return
    grad = cur["up"] / cur["dist"] * 100.0
    if grad < min_grad:
        return
    climbs.append({"start_km": round(cur["start_km"], 2),
                   "len_km": round(cur["dist"] / 1000.0, 2),
                   "grad_pct": round(grad, 1),
                   "dplus_m": round(cur["up"])})


def detect_climbs(segs, min_grad=CLIMB_MIN_GRAD, min_len_m=CLIMB_MIN_LEN,
                  min_dplus=CLIMB_MIN_DPLUS):
    """Montées principales du parcours → liste de dicts (start_km, len_km,
    grad_pct, dplus_m), triée par km de départ."""
    climbs = []
    cur = None
    for _, s in segs.iterrows():
        g, d = float(s["gradient_pct"]), float(s["dist_m"])
        if g >= CLIMB_TRIG or (cur is not None and g >= CLIMB_SOFT):
            if cur is None:
                cur = {"start_km": float(s["start_km"]), "dist": 0.0, "up": 0.0}
            cur["dist"] += d
            cur["up"] += d * g / 100.0
        else:
            if cur is not None:
                _push(climbs, cur, min_grad, min_len_m, min_dplus)
                cur = None
    if cur is not None:
        _push(climbs, cur, min_grad, min_len_m, min_dplus)
    return climbs


def climb_effort_max(grad_pct, len_m, cp, wprime, mass, scx, rho, wind_ms=0.0):
    """Effort max type « record » sur une montée : point fixe entre
    P(t) = CP + W′/t et l'équilibre physique de la pente.
    Retourne (temps_s, puissance_W)."""
    t = len_m / max(solve_speed(cp * 1.2, grad_pct, mass, scx, rho,
                                headwind_ms=wind_ms), 0.3)
    p = cp + wprime / max(t, 20.0)
    for _ in range(40):
        v = solve_speed(p, grad_pct, mass, scx, rho, headwind_ms=wind_ms)
        t_new = len_m / max(v, 0.2)
        p_new = cp + wprime / max(t_new, 20.0)
        if abs(t_new - t) < 0.2:
            t, p = t_new, p_new
            break
        t, p = t_new, p_new
    return t, p


def climb_time_at_power(grad_pct, len_m, power_w, mass, scx, rho, wind_ms=0.0):
    """Temps (s) sur une montée à puissance fixée."""
    v = solve_speed(power_w, grad_pct, mass, scx, rho, headwind_ms=wind_ms)
    return len_m / max(v, 0.3)


def power_for_time(grad_pct, len_m, time_s, mass, scx, rho, wind_ms=0.0,
                  crr=0.004, eta=0.976):
    """Puissance nécessaire (W) pour boucler une montée en time_s."""
    v = len_m / max(float(time_s), 1.0)
    from core.powermodel import G
    v_air = max(0.0, v + wind_ms)
    return (0.5 * rho * scx * v_air ** 2 * v
            + mass * G * (grad_pct / 100.0 + crr) * v) / eta


def race_plan(climbs, rest_km, grad_rest, rider, role, scenario="Normal",
              rho=1.225, scx=0.22, wind_ms=0.0, fatigue_pct=0.0, records=None,
              leader_record=False, draft=0.30):
    """Temps et énergie d'un parcours pour un coureur.

    climbs : montées [(start_km, len_km, grad_pct)] ; rest_km : km hors
    montées ; grad_rest : pente moyenne du roulant (%).
    rider : {"cp", "wprime", "mass_total"}.
    Leader : montées à l'effort max (CP + W′/t), fatigué si fatigue_pct > 0
    (réduction proportionnelle de CP et W′).
    Retourne dict : total_s, kj, avg_w, climbs=[(time_s, power_w)], rest_s,
    rest_w.
    """
    cp = float(rider["cp"])
    wp = float(rider["wprime"])
    mass = float(rider["mass_total"])
    fat = 1.0 - fatigue_pct / 100.0
    cp_f, wp_f = cp * fat, wp * fat

    flat_pct = FLAT_PCT.get(role, 0.55) * SCEN_FLAT.get(scenario, 1.0)
    rest_w = cp * flat_pct
    rest_s = 0.0
    if rest_km > 0:
        # Peloton : abri réduit le SCx effectif sur le roulant (~30 % par défaut)
        v = solve_speed(rest_w, grad_rest, mass, scx * (1.0 - draft), rho,
                        headwind_ms=wind_ms)
        rest_s = rest_km * 1000.0 / max(v, 0.3)

    det, total_j, climb_s = [], rest_w * rest_s, 0.0
    for i, (start_km, len_km, grad) in enumerate(climbs):
        rec = records[i] if records and i < len(records) else None
        if role == "Leader" and leader_record and rec:
            # Objectif explicite : le record relevé (Strava/KOM), alourdi par la fatigue
            t = rec / fat
            p = power_for_time(grad, len_km * 1000.0, t, mass, scx, rho, wind_ms)
        elif role == "Leader":
            t, p = climb_effort_max(grad, len_km * 1000.0, cp_f, wp_f, mass,
                                    scx, rho, wind_ms)
            if rec:
                t = rec / fat
                p = power_for_time(grad, len_km * 1000.0, t, mass, scx, rho, wind_ms)
        else:
            p = cp * CLIMB_PCT.get(role, 0.8)
            t = climb_time_at_power(grad, len_km * 1000.0, p, mass, scx, rho,
                                    wind_ms)
        det.append({"start_km": start_km, "len_km": len_km, "grad_pct": grad,
                    "time_s": t, "power_w": p})
        climb_s += t
        total_j += p * t

    total_s = climb_s + rest_s
    kj = total_j / 1000.0
    avg_w = total_j / max(total_s, 1.0)
    return {"total_s": total_s, "rest_s": rest_s, "rest_w": rest_w,
            "climbs": det, "kj": kj, "avg_w": avg_w,
            "avg_wkg": avg_w / max(mass - 8.0, 30.0)}


def cho_grams_per_hour(duration_h, avg_wkg, scenario="Normal"):
    """Recommandation glucides (g/h) — consensus récent, ajusté par
    intensité (W/kg) et scénario de course. Transport mixte glucose:fructose
    (~1:0,8) conseillé au-delà de 2h30."""
    if duration_h < 0.75:
        base = 30.0
    elif duration_h < 1.25:
        base = 60.0
    elif duration_h < 2.5:
        base = 80.0
    else:
        base = 100.0
    if scenario == "Rapide" or avg_wkg >= 3.6:
        base += 15.0
    elif scenario == "Lent" or avg_wkg < 2.6:
        base -= 15.0
    return float(np.clip(base, 20.0, 120.0))


def hydratation_l_per_h(duration_h, avg_wkg):
    """Hydratation conseillée (L/h), 0,4 à 0,9 selon durée/intensité."""
    return float(np.clip(0.5 + 0.08 * duration_h + 0.05 * max(avg_wkg - 3.0, 0.0),
                         0.4, 0.9))
