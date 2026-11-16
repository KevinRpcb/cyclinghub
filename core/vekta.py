"""Connecteur API Vekta (https://api.joinvekta.com) avec repli sur des données de démo.

La clé se génère dans Vekta → Settings → API keys et se colle dans les Secrets
Streamlit (VEKTA_API_KEY). Tant que l'API ne répond pas ou que la clé est
absente, les pages affichent des données de démonstration clairement signalées
via client.last_error.

Règles respectées d'après la spec OpenAPI publique :
- durées en secondes, distances en mètres, vitesses en m/s ;
- une valeur manquante est null côté API : on n'expose jamais de None ni de 0 ;
- 403 subscription_required = pas d'abonnement actif (pas une panne) ;
- 429 : on respecte Retry-After une seule fois.
"""
import hashlib
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

from .config import vekta_base_url, vekta_key

PP_DURATIONS = [("p10s", 10), ("p20s", 20), ("p30s", 30), ("p1m", 60),
                ("p3m", 180), ("p5m", 300), ("p10m", 600), ("p20m", 1200)]
RIDE_WORDS = ("ride", "cycl", "bike")
RACE_WORDS = ("race", " tt", "itt", "chrono", "clm", "prologue",
              "championnat", "championship", "crit", "contre-la-montre")

DEMO_ATHLETES = [
    {"id": "rider_a", "name": "A. COMBLUX", "role": "Grimpeur", "weight": 61.5, "hrmax": 188,
     "power_profile": {"cp": 415, "p10s": 1080, "p20s": 1010, "p30s": 950, "p1m": 780,
                       "p3m": 600, "p5m": 530, "p10m": 480, "p20m": 435}},
    {"id": "rider_b", "name": "B. VANDERPOOL", "role": "Rouleur CLM", "weight": 70.0, "hrmax": 182,
     "power_profile": {"cp": 430, "p10s": 990, "p20s": 940, "p30s": 900, "p1m": 760,
                       "p3m": 620, "p5m": 560, "p10m": 510, "p20m": 465}},
    {"id": "rider_c", "name": "C. FERRARI", "role": "Sprinteur", "weight": 76.5, "hrmax": 176,
     "power_profile": {"cp": 380, "p10s": 1480, "p20s": 1320, "p30s": 1150, "p1m": 850,
                       "p3m": 570, "p5m": 490, "p10m": 430, "p20m": 385}},
]


def _num(x):
    """Convertit une valeur API en float, ou None si absente/invalide (jamais 0 forcé)."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if np.isfinite(x) else None
    if isinstance(x, str):
        try:
            v = float(x.replace(",", "."))
            return v if np.isfinite(v) else None
        except ValueError:
            return None
    if isinstance(x, dict):
        for k in ("value", "avg", "mean", "amount", "val", "current"):
            if k in x:
                return _num(x[k])
    return None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _find_key(d: dict, *words):
    """Première clé de d contenant tous les mots (insensible casse/espaces/underscores)."""
    for k, v in d.items():
        lk = str(k).lower().replace(" ", "").replace("_", "").replace("-", "")
        if all(w in lk for w in words):
            return v
    return None


def _row_date(row: dict) -> str:
    for k in ("date", "startDate", "startTime", "start", "activityDate", "day"):
        if row.get(k):
            return str(row.get(k))[:10]
    return ""


def _rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("rows", "data", "items", "values", "metrics", "wellness",
                  "sleeps", "activities", "efforts", "bestEfforts"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _profile_from_cp(cp: float, wprime=None, maxpower=None) -> dict:
    """PPR dérivé du modèle critique P(t) = CP + W'/t, plafonné à la puissance max."""
    prof = {"cp": int(round(cp))}
    for key, t in PP_DURATIONS:
        p = cp + (wprime / t if wprime else 0.0)
        if maxpower:
            p = min(p, maxpower)
        prof[key] = int(round(p))
    return prof


def _extract_latest_metrics(profile: dict) -> dict:
    """Scan défensif de latestMetrics : critical power, w prime, max power, vo2max."""
    lm = profile.get("latestMetrics") if isinstance(profile, dict) else None
    if not isinstance(lm, dict):
        lm = profile if isinstance(profile, dict) else {}
    cp = _num(_find_key(lm, "critical", "power"))
    wprime = _num(_find_key(lm, "prime"))
    maxpower = _num(_find_key(lm, "max", "power"))
    vo2max = _num(_find_key(lm, "vo2"))
    return _clean({"cp": cp, "wprime": wprime, "maxpower": maxpower, "vo2max": vo2max})


def _map_athlete(u: dict) -> dict:
    uid = u.get("id", u.get("userId", u.get("uuid")))
    if uid is None:
        return {}
    first, last = u.get("firstName") or "", u.get("lastName") or ""
    name = (u.get("name") or u.get("fullName") or u.get("displayName")
            or f"{first} {last}".strip() or str(uid))
    return _clean({
        "id": str(uid),
        "name": str(name),
        "role": str(u.get("role") or u.get("type") or ""),
        "weight": _num(_find_key(u, "weight")),
        "hrmax": _num(_find_key(u, "maxheart")) or _num(_find_key(u, "hrmax"))
                 or _num(_find_key(u, "maxhr")),
    })


def _demo_metrics(athlete: dict) -> dict:
    seed = int(hashlib.md5(athlete["id"].encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    days = pd.date_range(end=pd.Timestamp.today().normalize(), periods=30)
    baseline = {"rider_a": 65, "rider_b": 72, "rider_c": 58}.get(athlete["id"], 65)
    vfc = baseline + np.cumsum(rng.normal(0, 1.2, 30)) * 0.4 + rng.normal(0, 2.2, 30)
    vfc = np.clip(vfc, baseline * 0.55, baseline * 1.25)
    series = [{"date": d.strftime("%Y-%m-%d"), "vfc": round(float(v), 1)} for d, v in zip(days, vfc)]
    last = float(vfc[-1])
    return {
        "vfc_last": round(last, 1),
        "vfc_baseline": baseline,
        "vfc_series": series,
        "vfc_trend_7d": round(float(np.mean(vfc[-7:]) - np.mean(vfc[-14:-7])), 1),
        "decouplage_cardio": round(float(abs(rng.normal(2.2, 1.4))), 1),
        "sleep_score": int(rng.integers(62, 94)),
        "hr_rest": int(rng.integers(38, 52)),
        "load_7d": int(rng.integers(600, 1050)),
        "load_28d": int(rng.integers(2400, 3800)),
        "ctl": round(float(rng.uniform(80, 125)), 1),
        "atl": round(float(rng.uniform(70, 130)), 1),
    }


def _demo_load(athlete: dict, days: int = 120) -> list:
    seed = int(hashlib.md5(("load" + athlete["id"]).encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=days)
    base = {"rider_a": 70, "rider_b": 78, "rider_c": 55}.get(athlete["id"], 65)
    load = np.clip(rng.normal(base, 32, days) + 12 * (rng.random(days) > 0.82), 0, None)
    return [{"date": d.strftime("%Y-%m-%d"), "load": round(float(v), 1)} for d, v in zip(dates, load)]


def _demo_efforts(athlete: dict) -> list:
    seed = int(hashlib.md5(("tt" + athlete["id"]).encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    pp = athlete["power_profile"]
    efforts = []
    today = datetime.today()
    for i in range(10):
        d = today - timedelta(days=int(7 * i + rng.integers(0, 4)))
        dur = float(rng.choice([600, 900, 1200, 1800, 2700, 3600]))
        intensity = 0.88 + 0.14 * rng.random()
        avg = pp["cp"] * (1.0 + (pp["p20m"] / pp["cp"] - 1.0) * (900.0 / dur)) * intensity
        efforts.append({
            "date": d.strftime("%Y-%m-%d"),
            "type": "course" if rng.random() > 0.45 else "entrainement",
            "duration_s": int(dur),
            "avg_w": int(avg),
            "np_w": int(avg * (1.02 + 0.06 * rng.random())),
            "speed_kmh": round(float(rng.uniform(44, 51)), 1),
            "gradient_pct": round(float(rng.normal(0.2, 0.6)), 1),
        })
    return efforts


def _volume_from_rows(rows: list) -> dict:
    sessions = hours = km = kj = tss = 0.0
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        n += 1
        dur = _num(_find_key(row, "duration"))
        if dur:
            hours += float(dur) / 3600.0
        dist = _num(_find_key(row, "distance"))
        if dist:
            km += float(dist) / 1000.0
        k = (_num(_find_key(row, "kj")) or _num(_find_key(row, "joule"))
             or _num(_find_key(row, "energy")))
        if k:
            kj += float(k)
        t = _num(_find_key(row, "tss"))
        if t:
            tss += float(t)
    return {"sessions": n, "hours": round(hours, 1), "km": round(km),
            "kj": round(kj), "tss": round(tss)}


def _demo_volume(athlete: dict, days: int) -> dict:
    seed = int(hashlib.md5(("vol" + athlete["id"]).encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    sessions = max(3, int(round(days * rng.uniform(0.55, 0.85))))
    hours = round(days * rng.uniform(1.6, 2.6), 1)
    km = round(hours * rng.uniform(34, 40))
    kj = round(hours * 3600 * rng.uniform(0.20, 0.28))
    tss = round(days * rng.uniform(28, 42))
    return {"sessions": sessions, "hours": hours, "km": km, "kj": kj, "tss": tss}


class VektaClient:
    """Client HTTP Vekta. Chaque méthode retombe sur la démo si l'API échoue."""

    def __init__(self):
        self.api_key = vekta_key()
        self.base_url = vekta_base_url().rstrip("/")
        self.last_error = None
        self.notice = None
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        self._session.headers.update({"Accept": "application/json"})
        self.athletes = list(DEMO_ATHLETES)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict = None):
        if not self.api_key:
            raise RuntimeError("VEKTA_API_KEY absente des Secrets")
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            r = self._session.get(url, params=params, timeout=12)
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = min(30.0, max(1.0, float(r.headers.get("Retry-After", "2"))))
                except (TypeError, ValueError):
                    wait = 2.0
                time.sleep(wait)
                continue
            break
        if r.status_code < 400:
            return r.json()
        code = ""
        try:
            err = r.json().get("error")
            if isinstance(err, dict):
                code = str(err.get("code", ""))
            elif err:
                code = str(err)
        except Exception:
            pass
        detail = f" ({code})" if code else ""
        if r.status_code == 401:
            raise RuntimeError("clé Vekta refusée (401) — vérifie VEKTA_API_KEY dans les Secrets")
        if r.status_code == 404:
            raise LookupError(f"introuvable dans Vekta (404){detail}")
        if r.status_code == 403:
            raise PermissionError(f"accès refusé par Vekta (403{detail})")
        raise RuntimeError(f"erreur Vekta {r.status_code}{detail}")

    def _list(self, path: str, params: dict = None) -> list:
        return _rows(self._get(path, params))

    def _fallback(self, err):
        self.last_error = str(err)[:180]

    def _date_range(self, days: int):
        today = pd.Timestamp.today().normalize()
        return ((today - pd.Timedelta(days=days - 1)).strftime("%Y-%m-%d"),
                today.strftime("%Y-%m-%d"))

    def _self_rider(self):
        """Profil 'me' (ton propre compte Vekta), utilisable quand la liste d'athlètes est vide."""
        try:
            prof = self._get("/users/me")
            mapped = _map_athlete(prof if isinstance(prof, dict) else {})
            if not mapped.get("id"):
                return None
            mapped["id"] = "me"
            mapped["role"] = "Moi (ton compte Vekta)"
            lm = _extract_latest_metrics(prof)
            if lm.get("cp"):
                mapped["power_profile"] = _profile_from_cp(
                    lm["cp"], lm.get("wprime"), lm.get("maxpower"))
            return mapped
        except Exception:
            return None

    def get_athletes(self) -> list:
        try:
            riders = [_map_athlete(u) for u in self._list("/users")]
            riders = [r for r in riders if r.get("id")]
            if not riders:
                me = self._self_rider()
                if me:
                    self.athletes = [me]
                    self.notice = ("Clé Vekta valide, mais aucun athlète abonné dans tes "
                                   "workspaces — tes propres données sont affichées. Un "
                                   "coureur n'apparaît que s'il a un abonnement Vekta actif.")
                    return self.athletes
                raise RuntimeError("clé Vekta valide mais liste d'athlètes vide, et ton "
                                   "propre compte n'a pas d'abonnement Vekta actif")
            self.athletes = riders
            return riders
        except Exception as e:
            self._fallback(e)
            return DEMO_ATHLETES

    def _cached(self, athlete_id: str):
        for a in self.athletes:
            if a.get("id") == athlete_id:
                return a
        return None

    def get_athlete(self, athlete_id: str) -> dict:
        if self.api_key:
            try:
                prof = self._get(f"/users/{athlete_id}")
                mapped = _map_athlete(prof if isinstance(prof, dict) else {})
                lm = _extract_latest_metrics(prof if isinstance(prof, dict) else {})
                if lm.get("cp"):
                    mapped["power_profile"] = _profile_from_cp(
                        lm["cp"], lm.get("wprime"), lm.get("maxpower"))
                merged = dict(self._cached(athlete_id) or {})
                merged.update(mapped)
                merged = _clean(merged)
                if self._cached(athlete_id):
                    self.athletes = [merged if a.get("id") == athlete_id else a
                                     for a in self.athletes]
                else:
                    self.athletes = self.athletes + [merged]
                return merged
            except Exception as e:
                self._fallback(e)
        cached = self._cached(athlete_id)
        return cached or self.athletes[0]

    def get_power_profile(self, athlete_id: str) -> dict:
        athlete = self.get_athlete(athlete_id)
        prof = athlete.get("power_profile")
        if isinstance(prof, dict) and prof.get("cp"):
            return dict(prof)
        self.last_error = self.last_error or \
            "Critical Power / W' absentes des métriques Vekta — profil de démonstration"
        return dict(DEMO_ATHLETES[0]["power_profile"])

    def _hrv_metrics(self, athlete_id: str, start: str, end: str):
        try:
            rows = self._list(f"/users/{athlete_id}/wellness", {"from": start, "to": end})
        except Exception:
            return None
        pts = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rtype = str(row.get("type", "")).lower()
            if rtype and "hrv" not in rtype:
                continue
            val = _num(row.get("value"))
            date = _row_date(row)
            if val is not None and val > 0 and date:
                pts.append((date, val))
        if not pts:
            return None
        pts.sort()
        vals = [v for _, v in pts]
        trend = float(np.mean(vals[-7:]) - np.mean(vals[-14:-7])) if len(vals) >= 14 else 0.0
        return _clean({
            "vfc_last": round(vals[-1], 1),
            "vfc_baseline": round(float(np.median(vals)), 1),
            "vfc_series": [{"date": d, "vfc": round(v, 1)} for d, v in pts],
            "vfc_trend_7d": round(trend, 1),
        })

    def _sleep_score(self, athlete_id: str, start: str, end: str):
        try:
            rows = self._list(f"/users/{athlete_id}/sleeps", {"from": start, "to": end})
        except Exception:
            return None
        best = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            val = _num(row.get("value")) or _num(_find_key(row, "score"))
            date = _row_date(row)
            if val is None or not date or not (0 < val <= 100):
                continue
            if best is None or date > best[0]:
                best = (date, val)
        return round(best[1]) if best else None

    def _load_frame(self, athlete_id: str, days: int):
        try:
            start, end = self._date_range(days)
            rows = self._list(f"/users/{athlete_id}/activities", {"from": start, "to": end})
        except Exception:
            return None
        per_day = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            date = _row_date(row)
            dur = _num(_find_key(row, "duration"))
            if not date or dur is None or dur <= 0:
                continue
            per_day[date] = per_day.get(date, 0.0) + dur / 60.0
        if not per_day:
            return None
        idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=days)
        series = pd.Series(0.0, index=idx)
        for d, minutes in per_day.items():
            ts = pd.Timestamp(d)
            if ts in series.index:
                series[ts] = minutes
        return pd.DataFrame({"load": series.values}, index=idx)

    def get_metrics(self, athlete_id: str, days: int = 30) -> dict:
        athlete = self.get_athlete(athlete_id)
        try:
            start, end = self._date_range(days)
            out = {}
            hrv = self._hrv_metrics(athlete_id, start, end)
            if hrv:
                out.update(hrv)
            sleep = self._sleep_score(athlete_id, start, end)
            if sleep is not None:
                out["sleep_score"] = sleep
            load = self._load_frame(athlete_id, max(days, 120))
            if load is not None:
                ctl = load["load"].ewm(span=42, adjust=False).mean()
                atl = load["load"].ewm(span=7, adjust=False).mean()
                out["ctl"] = round(float(ctl.iloc[-1]), 1)
                out["atl"] = round(float(atl.iloc[-1]), 1)
                out["load_7d"] = int(load["load"].tail(7).sum())
                out["load_28d"] = int(load["load"].tail(28).sum())
            if not out:
                raise RuntimeError(
                    f"aucune métrique lisible dans Vekta ({days} derniers jours)")
            return out
        except Exception as e:
            self._fallback(e)
            return _demo_metrics(athlete)

    def get_daily_load(self, athlete_id: str, days: int = 120) -> list:
        athlete = self.get_athlete(athlete_id)
        frame = self._load_frame(athlete_id, days)
        if frame is not None:
            return [{"date": d.strftime("%Y-%m-%d"), "load": round(float(v), 1)}
                    for d, v in zip(frame.index, frame["load"])]
        self.last_error = self.last_error or "Charge Vekta indisponible — données de démonstration"
        return _demo_load(athlete, days)

    def get_volume(self, athlete_id: str, days: int = 30, offset_days: int = 0) -> dict:
        """Volume brut sur une fenêtre : séances, heures, km, kJ, charge."""
        try:
            end = pd.Timestamp.today().normalize() - pd.Timedelta(days=offset_days)
            start = end - pd.Timedelta(days=days - 1)
            rows = self._list(f"/users/{athlete_id}/activities",
                              {"from": start.strftime("%Y-%m-%d"),
                               "to": end.strftime("%Y-%m-%d")})
            if not rows:
                raise RuntimeError("aucune activité lisible dans Vekta pour la fenêtre")
            return _volume_from_rows(rows)
        except Exception as e:
            self._fallback(e)
            return _demo_volume(self.get_athlete(athlete_id), days)

    def get_tt_efforts(self, athlete_id: str, days: int = 180, offset_days: int = 0) -> list:
        try:
            end = pd.Timestamp.today().normalize() - pd.Timedelta(days=offset_days)
            start = end - pd.Timedelta(days=days - 1)
            rows = self._list(f"/users/{athlete_id}/activities",
                              {"from": start.strftime("%Y-%m-%d"),
                               "to": end.strftime("%Y-%m-%d")})
        except Exception as e:
            self._fallback(e)
            return _demo_efforts(self.get_athlete(athlete_id))
        rides = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sport = str(row.get("sport", row.get("type", row.get("activityType", "")))).lower()
            if not any(w in sport for w in RIDE_WORDS):
                continue
            date = _row_date(row)
            dur = _num(_find_key(row, "duration"))
            if not date or not dur or dur < 300 or row.get("id") is None:
                continue
            rides.append((date, row, dur))
        rides.sort(key=lambda x: x[0], reverse=True)
        efforts = []
        for date, row, _dur in rides[:12]:
            try:
                be = self._get(f"/activities/{row['id']}/best-efforts")
            except Exception:
                continue
            for item in _rows(be):
                if not isinstance(item, dict):
                    continue
                d = _num(_find_key(item, "duration")) or _num(_find_key(item, "seconds"))
                w = _num(_find_key(item, "power")) or _num(_find_key(item, "watts"))
                if not d or not w or d < 60 or d > 7200 or w < 50:
                    continue
                name = str(row.get("name", row.get("title", ""))).lower()
                speed = _num(_find_key(row, "avgspeed"))
                efforts.append(_clean({
                    "date": date,
                    "type": "course" if any(x in name for x in RACE_WORDS) else "entrainement",
                    "duration_s": int(d),
                    "avg_w": int(w),
                    "np_w": int(_num(_find_key(item, "normalized", "power")) or w),
                    "speed_kmh": round(speed * 3.6, 1) if speed else None,
                    "gradient_pct": 0.0,
                }))
        return efforts
