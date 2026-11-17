"""Analyse des tests lactate : seuils SV1/SV2, zones d'entraînement, test SV1 3×15'.

Méthodes : SV1 (premier seuil) = lactate 2 mmol/L fixe ou hausse +0,5/+1,0
mmol/L au-dessus de la valeur de base du test ; SV2 = OBLA 4 mmol/L.
Puissances et FC aux seuils interpolées linéairement entre les paliers.
"""
import numpy as np

SV2_FIXED = 4.0
SV1_METHODS = {"2 mmol/L fixe": 2.0, "base + 0,5": 0.5, "base + 1,0": 1.0}


def _is_num(v):
    return v is not None and not (isinstance(v, float) and np.isnan(v))


def clean_steps(steps):
    """[(puissance, fc, lactate)] → triés par puissance, nombres valides seulement."""
    out = []
    for p, hr, la in steps:
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        hr = float(hr) if _is_num(_try(hr)) else np.nan
        la = float(la) if _is_num(_try(la)) else np.nan
        out.append((p, hr, la))
    out.sort(key=lambda s: s[0])
    return out


def _try(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def baseline_lactate(steps):
    las = [la for _, _, la in clean_steps(steps) if not np.isnan(la)]
    return min(las) if las else None


def _interp_at(steps, target):
    """Premier franchissement de `target` : (puissance, fc) interpolés, ou None."""
    prev = None
    for p, hr, la in steps:
        if np.isnan(la):
            continue
        if la >= target:
            if prev is None:
                return p, hr
            p0, hr0, la0 = prev
            if la <= la0:
                return p, hr
            f = (target - la0) / (la - la0)
            hr_t = np.nan if (np.isnan(hr) or np.isnan(hr0)) else hr0 + f * (hr - hr0)
            return p0 + f * (p - p0), hr_t
        prev = (p, hr, la)
    return None


def sv1(steps, method="2 mmol/L fixe"):
    """(puissance, fc) au premier seuil, ou None."""
    steps = clean_steps(steps)
    base = baseline_lactate(steps)
    if base is None:
        return None
    target = SV1_METHODS.get(method, 2.0)
    if target < 1.0:
        target = base + target
    return _interp_at(steps, target)


def sv2(steps):
    """(puissance, fc) au second seuil (OBLA 4 mmol/L), ou None."""
    return _interp_at(clean_steps(steps), SV2_FIXED)


def zone_table(lo, hi):
    """5 zones ancrées sur SV1/SV2 : [(label, bas, haut)] (haut None = ouvert)."""
    if lo is None or hi is None or hi <= lo:
        return None
    mid = lo + 0.5 * (hi - lo)
    return [("Z1 · Récupération", None, lo),
            ("Z2 · Endurance", lo, mid),
            ("Z3 · Tempo", mid, hi),
            ("Z4 · Seuil", hi, hi * 1.05),
            ("Z5 · Au-dessus du seuil", hi * 1.05, None)]


def sv1_block_verdict(la):
    """Verdict d'un bloc de 15' d'après le lactate post-bloc."""
    if not _is_num(la) or np.isnan(float(la)):
        return "—"
    la = float(la)
    if la < 1.5:
        return "Sous SV1 — intensité très sûre"
    if la < 2.5:
        return "Autour de SV1 — intensité cible"
    if la < SV2_FIXED:
        return "Entre SV1 et SV2 — un peu dur pour du SV1"
    return "Au-dessus de SV2 — trop dur"


def sv1_trend(las):
    """Dérive entre le 1er et le dernier bloc : (constat, lecture)."""
    vals = [float(la) for la in las if _is_num(la) and not np.isnan(float(la))]
    if len(vals) < 2:
        return None
    d = vals[-1] - vals[0]
    if d > 0.8:
        return ("dérive franche du lactate",
                "pas d'état d'équilibre — cible trop haute, réduis la FC/la puissance")
    if d > 0.3:
        return ("légère dérive",
                "équilibre limite — cible un peu haute, surveille le 3e bloc")
    return ("lactate stable",
            "équilibre atteint — zone bien calée pour tenir 3 × 15'")


def zone_table_partial(lo):
    """Zones provisoires ancrées sur SV1 seul (SV2 non atteinte pendant le test)."""
    if not _is_num(lo) or np.isnan(float(lo)) or lo <= 0:
        return None
    return [("Z1 · Récupération", None, lo),
            ("Z2 · Endurance", lo, lo * 1.12),
            ("Z3+ · Provisoire — SV2 non atteinte", lo * 1.12, None)]
