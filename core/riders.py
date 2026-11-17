"""Lecture du fichier database_CP.csv partagé entre les pages.

Deux formats reconnus automatiquement :
- simple : une ligne par coureur, colonnes requises name, weight, cp ;
- base équipe (export « values ») : entête contenant « Athlete », colonnes
  HRmax, 80% HR, Weight et la courbe 5 sec … 1 hour. La CP est prise dans
  la colonne cp si elle existe, sinon estimée (puissance 1 h, puis modèle
  CP/W′, puis 40 min × 1,05, puis 20 min × 1,08).

Les riders sont mis en cache dans st.session_state["csv_riders"] pour
suivre de page en page (Échauffement ↔ Prévision Perf).
"""
import io
import re

import pandas as pd
import streamlit as st

REQUIRED = ("name", "weight", "cp")
OPTIONAL_NUM = ("hrmax", "wprime", "p5s", "p10s", "p15s", "p20s", "p30s",
                "p1m", "p2m", "p3m", "p5m", "p10m", "p15m", "p20m", "p30m",
                "p40m", "p60m")

_DUR_COLS = {"5 sec": 5, "10 sec": 10, "15 sec": 15, "30 sec": 30,
             "1 min": 60, "2 min": 120, "3 min": 180, "5 min": 300,
             "10 min": 600, "15 min": 900, "20 min": 1200, "30 min": 1800,
             "40 min": 2400, "1 hour": 3600}
_PKEY = {5: "p5s", 10: "p10s", 15: "p15s", 30: "p30s", 60: "p1m",
         120: "p2m", 180: "p3m", 300: "p5m", 600: "p10m", 900: "p15m",
         1200: "p20m", 1800: "p30m", 2400: "p40m", 3600: "p60m"}


def _num(v):
    try:
        return float(str(v).strip().replace("\u202f", "").replace(" ", "")
                     .replace(",", "."))
    except (TypeError, ValueError):
        return None


def _estimate_cp(points):
    """points = [(secondes, watts), …] ; retourne (cp, wprime|None)."""
    by_t = dict(points)
    for t, factor in ((3600, 1.0), (2400, 1.05), (1200, 1.08)):
        p = by_t.get(t, 0)
        if p > 0:
            return round(p * factor), None
    long_pts = [(t, p) for t, p in points if 600 <= t <= 3600]
    if len(long_pts) >= 3:
        xs = [1 / t for t, _ in long_pts]
        ys = [p for _, p in long_pts]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den > 0:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
            cp = my - slope * mx
            if 100 < cp < 700 and 0 < slope < 150000:
                return round(cp), round(slope)
    return None, None


def _parse_database(text, sep, header_idx):
    """Base équipe : plusieurs blocs côte à côte (base + zones « Rider »),
    colonnes dupliquées suffixées .1/.2 par pandas. Chaque champ est rattaché
    au nom de coureur (Athlete ou Rider) situé à sa gauche, puis les zones
    « Rider » complètent les coureurs de la base par correspondance de nom."""
    lines = text.splitlines()
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=sep,
                         dtype=str)
    except Exception as e:
        return [], f"Base équipe illisible : {e}"
    cols = []
    for c in df.columns:
        cl = str(c).strip().lower()
        base = re.sub(r"\.\d+$", "", cl)
        cols.append((str(c), base))
    fields = ("hrmax", "80% hr", "weight", "cp") + tuple(_DUR_COLS)
    owner, current = {}, None
    for raw_col, base in cols:
        if base in ("athlete", "rider"):
            current = raw_col
        elif base in fields and current:
            owner[raw_col] = current
    if not any(base == "athlete" for _, base in cols):
        return [], "Base équipe : colonne Athlete introuvable."

    def _profile(row, name_col):
        def col_of(base):
            return next((raw for raw, b in cols
                         if owner.get(raw) == name_col and b == base), None)
        hr = _num(row.get(col_of("hrmax")))
        w = _num(row.get(col_of("weight")))
        h80 = _num(row.get(col_of("80% hr")))
        cp = _num(row.get(col_of("cp")))
        pts = []
        out = {}
        for raw_col, base in cols:
            if owner.get(raw_col) != name_col or base not in _DUR_COLS:
                continue
            v = _num(row.get(raw_col))
            if v and v > 0:
                out[_PKEY[_DUR_COLS[base]]] = v
                pts.append((_DUR_COLS[base], v))
        if hr and 100 < hr < 230:
            out["hrmax"] = int(hr)
        if w and 30 < w < 120:
            out["weight"] = w
        if h80 and 80 < h80 < 220:
            out["hr80"] = h80
        if cp and cp > 0:
            out["cp"] = round(cp)
        out["_pts"] = pts
        return out

    riders, picked, skipped, methods = [], {}, 0, set()
    athlete_col = next(raw for raw, base in cols if base == "athlete")
    for _, row in df.iterrows():
        name = str(row.get(athlete_col, "")).strip()
        if not name or name.lower() in ("nan", "∅", "avg.", "max", "min"):
            continue
        riders.append([name, _profile(row, athlete_col)])
    for raw_col, base in cols:
        if base != "rider":
            continue
        for _, row in df.iterrows():
            nm = str(row.get(raw_col, "")).strip()
            if nm and nm.lower() not in ("nan", "rider", "∅"):
                prof = _profile(row, raw_col)
                if len(prof["_pts"]) >= 2:
                    picked[nm.lower()] = prof
    for name, prof in riders:
        extra = picked.get(name.lower())
        if extra:
            for k, v in extra.items():
                if k == "_pts":
                    continue
                prof.setdefault(k, v)
            if not prof["_pts"]:
                prof["_pts"] = extra["_pts"]
    out_riders = []
    for name, prof in riders:
        pts = prof.pop("_pts", [])
        r = {"name": name, "team": "Team", "hrmax": prof.get("hrmax", 180),
             "weight": prof.get("weight", 70.0)}
        if "hr80" in prof:
            r["hr80"] = prof["hr80"]
        for k in OPTIONAL_NUM:
            if k in prof:
                r[k] = prof[k]
        if "cp" in prof:
            r["cp"], wprime = prof["cp"], None
            methods.add("colonne CP")
        else:
            cp, wprime = _estimate_cp(pts)
            if not cp:
                skipped += 1
                continue
            r["cp"], r["wprime"] = cp, wprime
            methods.add("puissance 1 h" if dict(pts).get(3600) else
                        "modèle CP/W′ sur la courbe")
        out_riders.append(r)
    if not out_riders:
        return [], ("Base équipe reconnue mais aucune ligne exploitable "
                    "— il faut au moins un coureur avec une courbe de "
                    "puissance ou une colonne CP remplie.")
    note = f" (CP : {' + '.join(sorted(methods))}"
    if skipped:
        note += f" · {skipped} coureur(s) sans puissance écarté(s)"
    return out_riders, note + ")"


def parse_riders_csv(raw: bytes):
    """Retourne (riders, message) : liste de dicts + message d'info/erreur vide."""
    try:
        text = raw.decode("utf-8-sig")
        sep = ";" if ";" in text.splitlines()[0] else ","
    except Exception as e:
        return [], f"CSV illisible : {e}"
    header_idx = next((i for i, ln in enumerate(text.splitlines())
                       if "athlete" in ln.lower()), None)
    if header_idx is not None:
        return _parse_database(text, sep, header_idx)
    try:
        df = pd.read_csv(io.StringIO(text), sep=sep)
    except Exception as e:
        return [], f"CSV illisible : {e}"
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        found = ", ".join(df.columns[:6]) + ("…" if len(df.columns) > 6 else "")
        return [], (f"Colonnes manquantes : {', '.join(missing)} — ce fichier ne "
                    f"ressemble ni à une base coureurs (name/weight/cp) ni à un "
                    f"export « values ». Colonnes trouvées : {found}")
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
