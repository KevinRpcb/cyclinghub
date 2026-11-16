"""Analyse physiologique : PMC, VFC, readiness, commentaires par métrique."""
import numpy as np
import pandas as pd


def compute_pmc(load_rows: list) -> pd.DataFrame:
    """load_rows: [{"date","load"}] — renvoie Fitness(CTL,42j)/Fatigue(ATL,7j)/Forme(TSB)."""
    df = pd.DataFrame(load_rows)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["date"])
    df = df.sort_values("Date").rename(columns={"load": "Charge"})
    df["Fitness"] = df["Charge"].ewm(span=42, adjust=False).mean()
    df["Fatigue"] = df["Charge"].ewm(span=7, adjust=False).mean()
    df["Forme"] = df["Fitness"].shift(1) - df["Fatigue"].shift(1)
    return df.fillna(0)


def vfc_verdict(m: dict) -> str:
    """Croisement VFC / efficacité mécanique (juge de paix)."""
    vfc = float(m.get("vfc_last", 0) or 0)
    base = float(m.get("vfc_baseline", 0) or 0)
    dec = float(m.get("decouplage_cardio", 0) or 0)
    if not base:
        return "VFC indisponible."
    if vfc < base * 0.85:
        if dec < 4.0:
            return (f"⚠️ VFC basse ({vfc} ms vs baseline {base} ms) MAIS mécanique excellente "
                    f"(dérive {dec} %). Hyper-activation sympathique (mode course) — feu vert tactique.")
        return (f"🚨 VFC basse ({vfc} ms) ET perte d'efficacité (dérive {dec} %). "
                "Fatigue systémique — bloc d'assimilation urgent.")
    if vfc > base * 1.15:
        return (f"🔵 VFC supranormale ({vfc} ms). Supercompensation possible — profiter de la fenêtre "
                "pour un stimulus qualitatif, surveiller les signes de parasympathique excessif.")
    return f"✅ VFC dans la norme ({vfc} ms vs baseline {base} ms). Assimilation validée."


def readiness(m: dict) -> tuple:
    """Score de fraîcheur 0-100 avec composantes."""
    comps = {}
    vfc = float(m.get("vfc_last", 0) or 0)
    base = float(m.get("vfc_baseline", 0) or 0) or 1
    ratio = vfc / base
    comps["VFC vs baseline"] = int(np.clip(50 + (ratio - 1) * 250, 0, 100))
    dec = float(m.get("decouplage_cardio", 5) or 5)
    comps["Efficacité (découplage)"] = int(np.clip(100 - (dec - 1) * 18, 0, 100))
    tsb = float(m.get("ctl", 0)) - float(m.get("atl", 0))
    comps["Fraîcheur (TSB)"] = int(np.clip(50 + tsb * 2.5, 0, 100))
    sleep = float(m.get("sleep_score", 70) or 70)
    comps["Sommeil"] = int(sleep)
    load7 = float(m.get("load_7d", 0) or 0)
    load28 = float(m.get("load_28d", 1) or 1) / 4.0
    ratio_l = load7 / load28 if load28 else 1
    comps["Équilibre charge 7j/28j"] = int(np.clip(100 - abs(ratio_l - 1) * 160, 0, 100))
    score = int(np.mean(list(comps.values())))
    return score, comps


def metric_comments(m: dict) -> dict:
    """Commentaire automatique (règles expertes) pour chaque métrique lisible."""
    out = {}
    out["VFC"] = vfc_verdict(m)
    dec = float(m.get("decouplage_cardio", 0) or 0)
    if dec < 2:
        out["Découplage cardio"] = f"✅ {dec} % — très bonne tolérance à l'endurance, mécanique stable."
    elif dec < 5:
        out["Découplage cardio"] = (f"⚠️ {dec} % — dérive modérée : travailler l'endurance fondamentale "
                                    "en blocs > 3 h et vérifier l'hydratation/glucidique.")
    else:
        out["Découplage cardio"] = f"🚨 {dec} % — dérive élevée : signal de fatigue périphérique ou carence glucidique."
    ctl, atl = float(m.get("ctl", 0) or 0), float(m.get("atl", 0) or 0)
    tsb = ctl - atl
    if tsb < -20:
        out["Charge / TSB"] = f"🚨 TSB {tsb:.0f} — fatigue accumulée importante, risque de contre-performance."
    elif tsb < -5:
        out["Charge / TSB"] = f"🟡 TSB {tsb:.0f} — bloc de charge actif, surveiller la réponse nocturne."
    elif tsb > 15:
        out["Charge / TSB"] = f"🔵 TSB {tsb:.0f} — détraining possible si prolongé, re-stimuler sous 72 h."
    else:
        out["Charge / TSB"] = f"✅ TSB {tsb:.0f} — zone de performance aiguë."
    ratio = (m.get("load_7d", 0) or 0) / ((m.get("load_28d", 0) or 1) / 4.0)
    if ratio > 1.3:
        out["Charge 7j/28j"] = f"⚠️ Ratio {ratio:.2f} — pic de charge, augmenter la surveillance (VFC, sommeil)."
    elif ratio < 0.7:
        out["Charge 7j/28j"] = f"🔵 Ratio {ratio:.2f} — affûtage ou décharge en cours."
    else:
        out["Charge 7j/28j"] = f"✅ Ratio {ratio:.2f} — progression contrôlée."
    sleep = int(m.get("sleep_score", 0) or 0)
    if sleep < 65:
        out["Sommeil"] = f"⚠️ Score sommeil {sleep} — prioriser 9 h d'opportunité de sommeil, sieste 20-30 min."
    else:
        out["Sommeil"] = f"✅ Score sommeil {sleep} — récupération nocturne correcte."
    return out
