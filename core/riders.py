"""Lecture du fichier database_CP.csv partagé entre les pages.

Format : une ligne par coureur. Colonnes requises : name, weight, cp.
Colonnes optionnelles : hrmax, wprime, p10s, p20s, p30s, p1m, p3m, p5m,
p10m, p20m. Les riders sont mis en cache dans st.session_state["csv_riders"]
pour suivre de page en page (Échauffement ↔ Prévision Perf).
"""
import io

import pandas as pd
import streamlit as st

REQUIRED = ("name", "weight", "cp")
OPTIONAL_NUM = ("hrmax", "wprime", "p10s", "p20s", "p30s", "p1m", "p3m",
                "p5m", "p10m", "p20m")


def parse_riders_csv(raw: bytes):
    """Retourne (riders, message) : liste de dicts + message d'info/erreur vide."""
    try:
        text = raw.decode("utf-8-sig")
        sep = ";" if ";" in text.splitlines()[0] else ","
        df = pd.read_csv(io.StringIO(text), sep=sep)
    except Exception as e:
        return [], f"CSV illisible : {e}"
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        return [], (f"Colonnes manquantes : {', '.join(missing)} — colonnes trouvées : "
                    f"{', '.join(df.columns)}")
    for c in ("weight", "cp") + OPTIONAL_NUM:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    before = len(df)
    df = df.dropna(subset=["name", "weight", "cp"])
    df = df[df["cp"] > 0]
    if df.empty:
        return [], ("Aucune ligne exploitable — 'name', 'weight' et 'cp' doivent "
                    "contenir des nombres (pas de texte comme '400 W').")
    riders = df.to_dict("records")
    for r in riders:
        r.setdefault("hrmax", 180)
        r.setdefault("weight", 70.0)
        r.setdefault("team", "Team")
    note = "" if before == len(riders) else f" ({before - len(riders)} ligne(s) écartée(s))"
    return riders, note


def cached_csv():
    """CSV conservé en session (dict {"name","riders"}) ou None."""
    return st.session_state.get("csv_riders")


def store_csv(name: str, riders: list):
    st.session_state["csv_riders"] = {"name": name, "riders": riders}
