"""Connecteur API Vekta avec repli automatique sur des données de démonstration.

Le mapping exact des endpoints dépend de la documentation Vekta de l'équipe :
il est centralisé ici (VEKTA_BASE_URL + chemins ci-dessous) pour être ajusté
sans toucher au reste de l'application. Tant que l'API ne répond pas, les
pages affichent des données de démonstration clairement signalées.
"""
import hashlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

from .config import vekta_base_url, vekta_key

ENDPOINTS = {
    "athletes": "/athletes",
    "metrics": "/athletes/{id}/metrics/latest",
    "power_profile": "/athletes/{id}/power-profile",
    "daily_load": "/athletes/{id}/load",
    "tt_efforts": "/athletes/{id}/efforts?discipline=tt",
}

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


class VektaClient:
    """Client HTTP Vekta. Chaque méthode retombe sur la démo si l'API échoue."""

    def __init__(self):
        self.api_key = vekta_key()
        self.base_url = vekta_base_url().rstrip("/")
        self.last_error = None
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        self._session.headers.update({"Accept": "application/json"})
        self.athletes = DEMO_ATHLETES

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict = None):
        if not self.api_key:
            raise RuntimeError("VEKTA_API_KEY absente des Secrets")
        url = f"{self.base_url}{path}"
        r = self._session.get(url, params=params, timeout=12)
        r.raise_for_status()
        return r.json()

    def _fallback(self, err):
        self.last_error = str(err)[:180]

    def get_athletes(self) -> list:
        try:
            data = self._get(ENDPOINTS["athletes"])
            riders = data.get("athletes", data) if isinstance(data, dict) else data
            if isinstance(riders, list) and riders:
                self.athletes = riders
                return riders
            raise RuntimeError("Réponse Vekta inattendue (liste de coureurs vide)")
        except Exception as e:
            self._fallback(e)
            return DEMO_ATHLETES

    def get_athlete(self, athlete_id: str) -> dict:
        for a in self.athletes:
            if a.get("id") == athlete_id:
                return a
        return self.athletes[0]

    def get_metrics(self, athlete_id: str) -> dict:
        try:
            data = self._get(ENDPOINTS["metrics"].format(id=athlete_id))
            if isinstance(data, dict) and data:
                return data
            raise RuntimeError("Réponse Vekta inattendue (métriques)")
        except Exception as e:
            self._fallback(e)
            return _demo_metrics(self.get_athlete(athlete_id))

    def get_power_profile(self, athlete_id: str) -> dict:
        athlete = self.get_athlete(athlete_id)
        try:
            data = self._get(ENDPOINTS["power_profile"].format(id=athlete_id))
            if isinstance(data, dict) and data.get("cp"):
                return data
            raise RuntimeError("Réponse Vekta inattendue (profil de puissance)")
        except Exception as e:
            self._fallback(e)
            return dict(athlete.get("power_profile", DEMO_ATHLETES[0]["power_profile"]))

    def get_daily_load(self, athlete_id: str, days: int = 120) -> list:
        try:
            data = self._get(ENDPOINTS["daily_load"].format(id=athlete_id), {"days": days})
            rows = data.get("load", data) if isinstance(data, dict) else data
            if isinstance(rows, list) and rows:
                return rows
            raise RuntimeError("Réponse Vekta inattendue (charge)")
        except Exception as e:
            self._fallback(e)
            return _demo_load(self.get_athlete(athlete_id), days)

    def get_tt_efforts(self, athlete_id: str) -> list:
        try:
            data = self._get(ENDPOINTS["tt_efforts"].format(id=athlete_id))
            rows = data.get("efforts", data) if isinstance(data, dict) else data
            if isinstance(rows, list) and rows:
                return rows
            raise RuntimeError("Réponse Vekta inattendue (efforts CLM)")
        except Exception as e:
            self._fallback(e)
            return _demo_efforts(self.get_athlete(athlete_id))
