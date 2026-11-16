"""Parcours GPX/FIT, météo gratuite (Open-Meteo) et simulation de pacing type BestBikeSplit."""
import io
import math
import xml.etree.ElementTree as ET
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from .powermodel import solve_speed, normalized_power, wprime_balance

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


# ── Parsing GPX ──────────────────────────────────────────────────────────
def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_gpx(data: bytes) -> list:
    root = ET.fromstring(data)
    pts = []
    for el in root.iter():
        if _local(el.tag) != "trkpt":
            continue
        try:
            lat, lon = float(el.get("lat")), float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        ele, t = None, None
        for ch in el:
            l = _local(ch.tag)
            if l == "ele" and ch.text:
                try:
                    ele = float(ch.text)
                except ValueError:
                    pass
            elif l == "time" and ch.text:
                t = ch.text
        pts.append({"lat": lat, "lon": lon, "ele": ele, "time": t})
    return pts


# ── Parsing FIT ──────────────────────────────────────────────────────────
def parse_fit(data: bytes) -> list:
    import fitdecode

    def _read(names):
        pts = []
        with fitdecode.FitReader(io.BytesIO(data)) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage) \
                        or frame.name not in names:
                    continue

                def get(name, _f=frame):
                    try:
                        return _f.get_value(name)
                    except (KeyError, ValueError):
                        return None

                lat, lon = get("position_lat"), get("position_long")
                if lat is None or lon is None:
                    continue
                lat = lat * 180.0 / 2 ** 31
                lon = lon * 180.0 / 2 ** 31
                ele = get("enhanced_altitude") or get("altitude")
                pts.append({"lat": lat, "lon": lon, "ele": ele,
                            "time": get("timestamp")})
        return pts

    pts = _read(("record",))
    alt = _read(("course_point",))
    if len(alt) > len(pts):
        pts = alt
    return pts


def parse_course(data: bytes, name: str) -> list:
    low = name.lower()
    if low.endswith(".gpx"):
        pts = parse_gpx(data)
    elif low.endswith(".fit"):
        pts = parse_fit(data)
    else:
        raise ValueError("Format non supporté — utilisez .gpx ou .fit.")
    if len(pts) < 20:
        raise ValueError("Fichier trop pauvre (< 20 points).")
    return pts


# ── Géométrie ────────────────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def build_segments(points: list, bin_m: float = 25.0) -> pd.DataFrame:
    """Agrège le parcours en segments (~bin_m) avec pente lissée et cap."""
    rows = []
    prev = None
    dist_cum = 0.0
    for p in points:
        if prev is not None:
            d = haversine(prev["lat"], prev["lon"], p["lat"], p["lon"])
            if d < 0.5:
                continue
            dist_cum += d
            rows.append({"d": dist_cum, "ele": p["ele"] if p["ele"] is not None else np.nan,
                         "bearing": bearing(prev["lat"], prev["lon"], p["lat"], p["lon"])})
        prev = p
    if not rows:
        raise ValueError("Aucun déplacement exploitable dans le fichier.")
    df = pd.DataFrame(rows)
    if df["ele"].notna().sum() >= 5:
        df["ele"] = df["ele"].interpolate(limit_direction="both").rolling(15, center=True, min_periods=1).mean()
    else:
        df["ele"] = 0.0
    edges = np.arange(0, df["d"].iloc[-1] + bin_m, bin_m)
    dvals = df["d"].values
    evals = df["ele"].values
    bvals = df["bearing"].values
    idx = np.clip(np.searchsorted(dvals, edges), 0, len(df) - 1)
    d_at = np.minimum(edges, dvals[-1])
    segs = []
    prev_ele = float(evals[0])
    for i in range(len(edges) - 1):
        dd = float(d_at[i + 1] - d_at[i])
        if dd <= 0:
            continue
        a = int(idx[i])
        b = int(idx[i + 1]) if i + 1 < len(edges) - 1 else len(df)
        b = max(b, min(a + 1, len(df)))
        e_end = float(evals[b - 1])
        segs.append({
            "start_km": float(d_at[i]) / 1000.0,
            "dist_m": dd,
            "gradient_pct": float(np.clip((e_end - prev_ele) / dd * 100.0, -18.0, 22.0)),
            "bearing_deg": float(bvals[a:b].mean()),
            "ele": e_end,
        })
        prev_ele = e_end
    return pd.DataFrame(segs)


# ── Météo gratuite (Open-Meteo, sans clé) ────────────────────────────────
def fetch_weather(lat: float, lon: float, date: str = None, hour: int = 13) -> dict:
    params = {
        "latitude": round(lat, 3), "longitude": round(lon, 3),
        "hourly": "temperature_2m,surface_pressure,wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms", "timezone": "auto",
    }
    if date:
        params["start_date"] = params["end_date"] = date
    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        h = r.json()["hourly"]
        times = [datetime.fromisoformat(t) for t in h["time"]]
        if date:
            target_idx = min(range(len(times)), key=lambda i: abs(times[i].hour - hour))
        else:
            now = datetime.now(times[0].tzinfo) if times else None
            target_idx = min(range(len(times)), key=lambda i: abs((times[i] - now).total_seconds())) if now else 12
        return {
            "temp_c": float(h["temperature_2m"][target_idx]),
            "pressure_hpa": float(h["surface_pressure"][target_idx]),
            "wind_ms": float(h["wind_speed_10m"][target_idx]),
            "wind_dir": float(h["wind_direction_10m"][target_idx]),
            "time": times[target_idx].strftime("%d/%m %H:%M"),
        }
    except Exception as e:
        return {"error": f"Météo indisponible : {e}"}


def headwind_ms(wind_ms: float, wind_dir: float, seg_bearing: float) -> float:
    """Composante de vent effectif de face (positive = vent de face)."""
    return wind_ms * math.cos(math.radians(wind_dir - seg_bearing))


# ── Simulation de pacing ─────────────────────────────────────────────────
def simulate(segs: pd.DataFrame, mass_kg: float, scx: float, rho: float, cp: float,
             wprime: float, p_target: float = None, goal_time_s: float = None,
             k_up: float = 2.2, crr: float = 0.004, eta: float = 0.976,
             wind_ms: float = 0.0, wind_dir: float = 0.0) -> dict:
    """Répartit la puissance selon la pente/le vent, calibre p0 sur l'objectif.

    p_target : puissance moyenne visée (W). goal_time_s : temps objectif (s) —
    si fourni, p0 est calibré par bissection pour atteindre l'objectif.
    """
    grads = segs["gradient_pct"].values
    bears = segs["bearing_deg"].values
    dists = segs["dist_m"].values
    winds = np.array([headwind_ms(wind_ms, wind_dir, b) for b in bears])

    def run(p0):
        powers, speeds, times = [], [], []
        for g, d, w in zip(grads, dists, winds):
            p = p0 * (1.0 + k_up * g / 100.0)
            p = float(np.clip(p, max(60.0, 0.55 * p0), p0 * 3.5))
            v = solve_speed(p, g, mass_kg, scx, rho, crr, eta, w)
            powers.append(p)
            speeds.append(v)
            times.append(d / max(v, 0.3))
        return np.array(powers), np.array(speeds), np.array(times)

    if goal_time_s:
        lo, hi = cp * 0.5, cp * 2.5
        for _ in range(14):
            mid = 0.5 * (lo + hi)
            t_total = run(mid)[2].sum()
            if t_total > goal_time_s:
                lo = mid
            else:
                hi = mid
        p0 = 0.5 * (lo + hi)
    elif p_target:
        p0 = p_target
    else:
        p0 = cp

    powers, speeds, times = run(p0)
    bal = wprime_balance(powers.tolist(), times.tolist(), cp, wprime)
    out = segs.copy()
    out["power_w"] = np.round(powers).astype(int)
    out["speed_kmh"] = np.round(speeds * 3.6, 1)
    out["time_s"] = np.round(times, 1)
    out["wprime_left_j"] = np.round(bal).astype(int)
    total_s = float(times.sum())
    return {
        "df": out,
        "total_s": total_s,
        "avg_w": float(np.average(powers, weights=times)),
        "np_w": normalized_power(powers.tolist(), times.tolist()),
        "min_wprime_j": float(min(bal)),
        "p0": p0,
    }
