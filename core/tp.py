"""Pont TrainingPeaks — API interne tpapi.trainingpeaks.com (celle du site web et de WKO5).

Lecture seule : on ne crée, modifie ni supprime rien dans le compte TP.

Authentification (option choisie par le coach) : identifiants TP dans les
Secrets Streamlit (TP_USERNAME / TP_PASSWORD). L'app reproduit la connexion
du site web : GET home.trainingpeaks.com/login → jeton anti-CSRF → POST du
formulaire → cookie Production_tpAuth → échange contre un jeton Bearer via
GET /users/v3/token. Le cookie est ré-obtenu automatiquement à l'expiration.

API non officielle : elle peut changer sans préavis. Aucun identifiant n'est
affiché, loggé ou inclus dans une erreur.
"""
import re
import time
from datetime import date, timedelta

import requests
import streamlit as st

from .config import tp_password, tp_username

LOGIN_URL = "https://home.trainingpeaks.com/login"
BASE_URL = "https://tpapi.trainingpeaks.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FORM_HEADERS = {
    "User-Agent": _UA,
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://home.trainingpeaks.com",
    "Referer": LOGIN_URL,
}

PEAK_SLOTS = [("p1m", "power1min"), ("p5m", "power5min"),
              ("p10m", "power10min"), ("p20m", "power20min")]
BIKE_TYPE_IDS = {2, 8}
RACE_TYPE_IDS = {6}
TT_TITLE_WORDS = ("tt", "itt", "clm", "chrono", "contre-la-montre", "prologue", "time trial")


class TPError(RuntimeError):
    pass


def login(username: str, password: str) -> str:
    """Connexion programmatique au site TP : retourne le cookie Production_tpAuth."""
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    r = s.get(LOGIN_URL, timeout=15)
    if r.status_code != 200:
        raise TPError(f"page de connexion TP injoignable (HTTP {r.status_code})")
    m = (re.search(r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]*)"', r.text)
         or re.search(r'<input[^>]+value="([^"]*)"[^>]+name="__RequestVerificationToken"', r.text))
    if not m:
        raise TPError("page de connexion TP illisible (jeton anti-CSRF absent) — "
                      "l'API interne a peut-être changé")
    r2 = s.post(LOGIN_URL,
                data={"Username": username, "Password": password,
                      "__RequestVerificationToken": m.group(1)},
                headers=_FORM_HEADERS, timeout=20, allow_redirects=True)
    low = r2.text.lower()
    if ("invalid" in low and "password" in low) or "remember and failed" in low:
        raise TPError("identifiant ou mot de passe TrainingPeaks refusé")
    cookie = s.cookies.get("Production_tpAuth")
    if not cookie:
        raise TPError("connexion TP acceptée mais aucun cookie d'authentification "
                      "renvoyé — l'API interne a peut-être changé")
    return cookie


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace(",", "."))
        except ValueError:
            return None
    return None


def _map_person(p: dict) -> dict:
    pid = p.get("personId") or p.get("athleteId") or p.get("userId") or p.get("id")
    if pid is None:
        return {}
    name = (p.get("fullName") or f"{p.get('firstName') or ''} {p.get('lastName') or ''}".strip()
            or p.get("userName") or p.get("email") or str(pid))
    return {k: v for k, v in {
        "id": str(pid),
        "name": str(name),
        "role": str(p.get("role") or ""),
        "weight": _num(p.get("weight")),
        "hrmax": _num(p.get("maxHr")) or _num(p.get("maxHR")),
    }.items() if v is not None}


def _map_workout(w: dict) -> dict:
    dur_h = _num(w.get("totalTime")) or _num(w.get("totalTimePlanned"))
    return {k: v for k, v in {
        "date": str(w.get("workoutDay") or "")[:10],
        "title": str(w.get("title") or "Séance"),
        "type": str(w.get("workoutTypeValueId") or ""),
        "duration_h": round(dur_h, 3) if dur_h else None,
        "distance_km": round(_num(w.get("distance")) / 1000.0, 1)
                       if _num(w.get("distance")) else None,
        "tss": _num(w.get("tssActual")) if _num(w.get("tssActual")) is not None
               else _num(w.get("tssPlanned")),
        "avg_w": _num(w.get("powerAverage")),
        "avg_hr": _num(w.get("heartRateAverage")),
        "completed": bool(w.get("completed")),
    }.items() if v is not None}


class TPClient:
    """Client HTTP TrainingPeaks (lecture seule), même interface que VektaClient."""

    def __init__(self):
        self.username = tp_username()
        self.password = tp_password()
        self.last_error = None
        self.notice = None
        self._session = requests.Session()
        self._token = None
        self._token_exp = 0.0
        self._me = None
        self.athletes = []

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)

    def _fallback(self, err):
        self.last_error = str(err)[:180]

    def _date_range(self, days: int):
        today = date.today()
        return ((today - timedelta(days=days - 1)).strftime("%Y-%m-%d"),
                today.strftime("%Y-%m-%d"))

    def _connect(self):
        if not self.configured:
            raise TPError("TP_USERNAME / TP_PASSWORD absents des Secrets Streamlit")
        cookie = login(self.username, self.password)
        self._session.cookies.set("Production_tpAuth", cookie, domain="tpapi.trainingpeaks.com")
        self._token = None
        self._token_exp = 0.0

    def _ensure_token(self):
        if self._token and time.time() < self._token_exp - 60:
            return
        if "Production_tpAuth" not in self._session.cookies:
            self._connect()
        r = self._session.get(f"{BASE_URL}/users/v3/token", timeout=15)
        if r.status_code == 401:
            self._connect()
            r = self._session.get(f"{BASE_URL}/users/v3/token", timeout=15)
        if r.status_code != 200:
            raise TPError(f"authentification TrainingPeaks impossible (HTTP {r.status_code})")
        data = r.json()
        tok = data.get("token") if isinstance(data, dict) else None
        if not data.get("success") or not isinstance(tok, dict) or not tok.get("access_token"):
            raise TPError("réponse d'authentification TrainingPeaks inattendue")
        self._token = tok["access_token"]
        self._token_exp = time.time() + float(tok.get("expires_in", 3600))

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None):
        self._ensure_token()
        r = self._session.request(method, f"{BASE_URL}{path}", params=params,
                                  json=json_body, headers=self._headers(), timeout=20)
        if r.status_code == 401:
            self._connect()
            self._ensure_token()
            r = self._session.request(method, f"{BASE_URL}{path}", params=params,
                                      json=json_body, headers=self._headers(), timeout=20)
        if r.status_code == 404:
            raise LookupError("introuvable dans TrainingPeaks (404)")
        if r.status_code == 403:
            raise PermissionError("accès refusé par TrainingPeaks (403)")
        if r.status_code >= 400:
            raise TPError(f"erreur TrainingPeaks {r.status_code}")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _get(self, path: str, params: dict = None):
        return self._request("GET", path, params=params)

    def _post(self, path: str, json_body: dict):
        return self._request("POST", path, json_body=json_body)

    def _me_data(self) -> dict:
        if self._me is None:
            data = self._get("/users/v3/user")
            self._me = data.get("user", data) if isinstance(data, dict) else {}
        return self._me

    def get_athletes(self) -> list:
        """Le compte coach (toi) + les athlètes coachés, dédupliqués par ID."""
        self.last_error = None
        try:
            me = self._me_data()
            riders, seen = [], set()
            self_ = _map_person(me)
            if self_.get("id"):
                self_["role"] = "Moi (ton compte TP)"
                riders.append(self_)
                seen.add(self_["id"])
            for p in me.get("athletes") or []:
                if not isinstance(p, dict):
                    continue
                mapped = _map_person(p)
                if mapped.get("id") and mapped["id"] not in seen:
                    riders.append(mapped)
                    seen.add(mapped["id"])
            if not riders:
                raise TPError("aucun athlète visible depuis ce compte TP")
            self.athletes = riders
            return riders
        except Exception as e:
            self._fallback(e)
            return []

    def _cached(self, athlete_id: str):
        for a in self.athletes:
            if a.get("id") == str(athlete_id):
                return a
        return None

    def get_athlete(self, athlete_id: str) -> dict:
        cached = self._cached(athlete_id)
        if cached:
            return dict(cached)
        try:
            me = self._me_data()
            for p in [me] + [a for a in (me.get("athletes") or []) if isinstance(a, dict)]:
                mapped = _map_person(p)
                if mapped.get("id") == str(athlete_id):
                    self.athletes = self.athletes + [mapped]
                    return mapped
        except Exception as e:
            self._fallback(e)
        return {"id": str(athlete_id), "name": str(athlete_id)}

    def get_fitness(self, athlete_id, start: str, end: str) -> list:
        """Lignes journalières {workoutDay, tssActual, ctl, atl, tsb}."""
        data = self._post(f"/fitness/v1/athletes/{athlete_id}/reporting/performancedata/"
                          f"{start}/{end}",
                          {"atlConstant": 7, "atlStart": 0, "ctlConstant": 42,
                           "ctlStart": 0, "workoutTypes": []})
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return []

    def get_metrics(self, athlete_id: str, days: int = 120) -> dict:
        try:
            start, end = self._date_range(days)
            rows = self.get_fitness(athlete_id, start, end)
            if not rows:
                raise TPError(f"aucune métrique Fitness lisible dans TrainingPeaks "
                              f"({days} derniers jours)")
            rows.sort(key=lambda r: str(r.get("workoutDay") or ""))
            last = rows[-1]
            tss = [max(0.0, _num(r.get("tssActual")) or 0.0) for r in rows]
            return {
                "ctl": round(float(_num(last.get("ctl")) or 0.0), 1),
                "atl": round(float(_num(last.get("atl")) or 0.0), 1),
                "load_7d": int(sum(tss[-7:])),
                "load_28d": int(sum(tss[-28:])),
            }
        except Exception as e:
            self._fallback(e)
            return {}

    def get_daily_load(self, athlete_id: str, days: int = 120) -> list:
        try:
            start, end = self._date_range(days)
            rows = self.get_fitness(athlete_id, start, end)
            out = []
            for r in rows:
                day = str(r.get("workoutDay") or "")[:10]
                if day:
                    out.append({"date": day, "load": max(0.0, _num(r.get("tssActual")) or 0.0)})
            out.sort(key=lambda x: x["date"])
            return out
        except Exception as e:
            self._fallback(e)
            return []

    def get_workouts(self, athlete_id, start: str, end: str) -> list:
        try:
            data = self._get(f"/fitness/v6/athletes/{athlete_id}/workouts/{start}/{end}")
            rows = data if isinstance(data, list) else []
            return [_map_workout(w) for w in rows if isinstance(w, dict)]
        except Exception as e:
            self._fallback(e)
            return []

    def _peak(self, athlete_id, pr_type: str):
        data = self._get(f"/personalrecord/v2/athletes/{athlete_id}/Bike",
                         params={"prType": pr_type})
        rows = data if isinstance(data, list) else []
        best = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            rank = _num(r.get("rank"))
            val = _num(r.get("value"))
            if val is None or val <= 0:
                continue
            if best is None or (rank is not None and rank < best[0]):
                best = (rank if rank is not None else 999, val)
        return best[1] if best else None

    def get_cp_estimate(self, athlete_id) -> dict:
        """CP estimée en 2 appels seulement (puissance 60 min, sinon 95 % de 20 min)."""
        try:
            p60 = self._peak(athlete_id, "power60min")
            if p60:
                return {"cp": int(round(p60))}
            p20 = self._peak(athlete_id, "power20min")
            if p20:
                return {"cp": int(round(0.95 * p20))}
        except Exception as e:
            self._fallback(e)
        return {}

    def get_tt_efforts(self, athlete_id, days: int = 120) -> list:
        """Efforts vélo (fenêtre `days` jours) déduits des séances TP : date, durée, puissance.

        Un effort est retenu si la séance est de type vélo avec puissance moyenne
        et durée >= 5 min. 'course' si TP la marque Race, sinon 'entrainement'.
        """
        try:
            start, end = self._date_range(days)
            rows = self._get(f"/fitness/v6/athletes/{athlete_id}/workouts/{start}/{end}")
            rows = rows if isinstance(rows, list) else []
        except Exception as e:
            self._fallback(e)
            return []
        out = []
        for w in rows:
            if not isinstance(w, dict):
                continue
            wtype = w.get("workoutTypeValueId")
            title = str(w.get("title") or "").lower()
            is_tt = (wtype in BIKE_TYPE_IDS
                     or any(wd in title for wd in TT_TITLE_WORDS))
            if not is_tt:
                continue
            dur_h = _num(w.get("totalTime"))
            avg_w = _num(w.get("powerAverage"))
            if not dur_h or dur_h * 3600 < 300 or not avg_w or avg_w <= 0:
                continue
            vel = _num(w.get("velocityAverage"))
            out.append({k: v for k, v in {
                "date": str(w.get("workoutDay") or "")[:10],
                "type": "course" if wtype in RACE_TYPE_IDS else "entrainement",
                "duration_s": int(round(dur_h * 3600)),
                "avg_w": int(round(avg_w)),
                "np_w": int(round(_num(w.get("normalizedPowerActual"))))
                        if _num(w.get("normalizedPowerActual")) else None,
                "speed_kmh": round(vel * 3.6, 1) if vel else None,
                "gradient_pct": None,
            }.items() if v is not None})
        out.sort(key=lambda x: x.get("date", ""))
        return out

    def get_power_profile(self, athlete_id: str) -> dict:
        prof = {}
        try:
            for key, pr_type in PEAK_SLOTS:
                try:
                    val = self._peak(athlete_id, pr_type)
                except Exception:
                    val = None
                if val:
                    prof[key] = int(round(val))
            p20 = prof.get("p20m")
            if p20:
                try:
                    p60 = self._peak(athlete_id, "power60min")
                except Exception:
                    p60 = None
                prof["cp"] = int(round(p60 if p60 else 0.95 * p20))
        except Exception as e:
            self._fallback(e)
        return prof


def session_client() -> TPClient:
    """TPClient mis en cache dans la session : un seul login par session Streamlit."""
    if "tp_client" not in st.session_state:
        st.session_state["tp_client"] = TPClient()
    return st.session_state["tp_client"]
