"""Client Strava léger — reco d'étape : segments le long du parcours + records.

Authentification : l'équipe crée une application API sur Strava
(strava.com/settings/api) et stocke dans les Secrets Streamlit :
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
Le refresh token est obtenu une seule fois via le panneau « Connexion Strava »
de la page Prévision Course ; seul le refresh token se conserve (jamais dans
le code public).

Limites API : ~200 requêtes / 15 min, ~2000/jour (gratuit) — une étape de
200 km ≈ 50 zones d'exploration. La page met les résultats en cache.
"""
import time

import requests
from core.config import get_secret

BASE = "https://www.strava.com/api/v3"
OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
OAUTH_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"


class StravaError(Exception):
    """Erreur API Strava avec message clair pour l'UI."""


# ── Configuration depuis les Secrets ─────────────────────────────────────────
def is_configured() -> bool:
    return bool(get_secret("STRAVA_CLIENT_ID")
                and get_secret("STRAVA_CLIENT_SECRET")
                and get_secret("STRAVA_REFRESH_TOKEN"))


def oauth_client_ready() -> bool:
    """Client API créé (ID + Secret dans les Secrets) — prêt pour \nl'autorisation unique."""
    return bool(get_secret("STRAVA_CLIENT_ID")
                and get_secret("STRAVA_CLIENT_SECRET"))


def credentials() -> dict:
    return {"client_id": get_secret("STRAVA_CLIENT_ID"),
            "client_secret": get_secret("STRAVA_CLIENT_SECRET"),
            "refresh_token": get_secret("STRAVA_REFRESH_TOKEN")}


def authorize_url(redirect_uri: str = "http://localhost") -> str:
    cid = get_secret("STRAVA_CLIENT_ID")
    from urllib.parse import quote
    return (f"{OAUTH_AUTHORIZE_URL}?client_id={cid}&response_type=code"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&approval_prompt=auto&scope=read")


def exchange_code(code: str) -> dict:
    """Échange le code d'autorisation (connexion unique) contre les tokens."""
    r = requests.post(OAUTH_TOKEN_URL, data={
        "client_id": get_secret("STRAVA_CLIENT_ID"),
        "client_secret": get_secret("STRAVA_CLIENT_SECRET"),
        "code": code, "grant_type": "authorization_code",
    }, timeout=20)
    if r.status_code != 200:
        raise StravaError(f"Échange de code impossible (HTTP {r.status_code}).")
    d = r.json()
    return {"access_token": d.get("access_token", ""),
            "refresh_token": d.get("refresh_token", ""),
            "expires_at": d.get("expires_at", 0)}


# ── Client ───────────────────────────────────────────────────────────────────
class StravaClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        if not (client_id and client_secret and refresh_token):
            raise StravaError("Strava non configuré — Secrets manquants.")
        self._cid = client_id
        self._sec = client_secret
        self._refresh_token = refresh_token
        self._token = ""
        self._expires = 0.0
        self.calls = 0
        self.last_lb_error = ""

    def _ensure_token(self):
        if self._token and time.time() < self._expires - 60:
            return
        r = requests.post(OAUTH_TOKEN_URL, data={
            "client_id": self._cid, "client_secret": self._sec,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }, timeout=20)
        if r.status_code != 200:
            raise StravaError(
                f"Rafraîchissement du token impossible (HTTP {r.status_code}) "
                "— revérifie les Secrets Strava.")
        d = r.json()
        self._token = d.get("access_token", "")
        self._expires = float(d.get("expires_at", time.time() + 3500))
        new_refresh = d.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            # Strava peut renvoyer un refresh token : on réutilise celui-ci en
            # mémoire pour la suite de la session (pas de stockage disque).
            self._refresh_token = new_refresh

    def _get(self, path, params=None):
        self._ensure_token()
        r = requests.get(f"{BASE}/{path.lstrip('/')}", headers={
            "Authorization": f"Bearer {self._token}"}, params=params or {},
            timeout=25)
        self.calls += 1
        if r.status_code == 429:
            raise StravaError("Quota Strava atteint (200 req / 15 min) — "
                              "réessaie dans quelques minutes.")
        if r.status_code != 200:
            raise StravaError(f"Strava HTTP {r.status_code} sur /{path}.")
        return r.json()

    # ── Endpoints utilisés ──
    def explore_segments(self, south, west, north, east):
        """Segments publics dans la boîte (≤10 retournés par Strava)."""
        out = self._get("segments/explore", {
            "bounds": f"{south},{west},{north},{east}"})
        return out.get("segments", [])

    def leaderboard_kom(self, segment_id: int, sex: str = None):
        """(secondes, nom de l'athlète, date ISO) du record du segment,
        ou None si pas de classement accessible. sex = "M" ou "F" filtre
        le classement par genre (équipe hommes + femmes)."""
        self.last_lb_error = ""
        try:
            params = {"per_page": 1}
            if sex in ("M", "F"):
                params["gender"] = sex
            lb = self._get(f"segments/{segment_id}/leaderboard", params)
        except StravaError as e:
            self.last_lb_error = f"classement {sex or 'H'} : {str(e)[:160]}"
            return None
        entries = lb.get("entries", [])
        if not entries:
            self.last_lb_error = f"classement {sex or 'H'} : vide"
            return None
        e = entries[0]
        return {"time_s": e.get("elapsed_time"),
                "athlete": e.get("athlete_name", ""),
                "date": e.get("start_date", ""),
                "avg_speed_s": None}


# ── Reco d'étape : échantillonnage du parcours et zones d'exploration ────────
def _km_along(points):
    """km cumulé par point (haversine), même base que build_segments."""
    from core.course import haversine
    kms, tot = [0.0], 0.0
    for a, b in zip(points[:-1], points[1:]):
        tot += haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        kms.append(tot / 1000.0)
    return kms


def route_boxes(points, step_km: float = 4.0, pad_km: float = 1.0):
    """Découpe le parcours en boîtes geo pour segments/explore.

    Retourne [(south, west, north, east, km_centre)] — ~step_km entre chaque
    boîte, marge pad_km autour. Une étape de 200 km ≈ 50 requêtes."""
    kms = _km_along(points)
    boxes, i0 = [], 0
    n = len(points)
    while i0 < n - 1:
        j = i0
        while j < n - 1 and kms[j] - kms[i0] < step_km:
            j += 1
        win = points[i0:j + 1]
        lats = [p["lat"] for p in win]
        lons = [p["lon"] for p in win]
        boxes.append((min(lats) - pad_km / 111.0,
                      min(lons) - pad_km / (111.0 * 0.7),
                      max(lats) + pad_km / 111.0,
                      max(lons) + pad_km / (111.0 * 0.7),
                      (kms[i0] + kms[j]) / 2.0))
        i0 = j
    return boxes





def collect_segments(points, client: StravaClient, corridor_m: float = 800.0,
                     step_km: float = 4.0, progress=None):
    """Tous les segments le long du parcours (déduits, triés par km).

    points : trace du fichier course ; client : StravaClient authentifié.
    progress : callback optionnel (nb_zones_traitées, total).
    Retourne (segments, nb_zones) avec segments :
    [{id, name, km, len_m, avg_grade, dplus, dist_route_m, climb}]"""
    kms = _km_along(points)
    boxes = route_boxes(points, step_km=step_km, pad_km=1.0)
    seen, segs = set(), []
    for bi, (s, w, n, e, _kmbox) in enumerate(boxes):
        if progress:
            progress(bi, len(boxes))
        for sg in client.explore_segments(s, w, n, e):
            sid = sg.get("id")
            if sid in seen:
                continue
            seen.add(sid)
            sl = sg.get("start_latlng") or [None, None]
            kla, klo = sl[0], sl[1]
            if kla is None or klo is None:
                continue
            # point de trace le plus proche -> km sur le parcours
            best_d, best_i = 1e18, 0
            for i in range(0, len(points), 2):
                p = points[i]
                d2 = (p["lat"] - kla) ** 2 + (p["lon"] - klo) ** 2
                if d2 < best_d:
                    best_d, best_i = d2, i
            import math
            mlat = 111320.0
            mlon = 111320.0 * math.cos(math.radians(kla))
            dm = math.sqrt(((points[best_i]["lat"] - kla) * mlat) ** 2
                           + ((points[best_i]["lon"] - klo) * mlon) ** 2)
            if dm > corridor_m:
                continue
            grade = float(sg.get("average_grade") or 0.0)
            segs.append({
                "id": sid, "name": sg.get("name", f"Segment {sid}"),
                "km": round(kms[best_i], 2),
                "len_m": float(sg.get("distance") or 0.0),
                "avg_grade": grade,
                "dplus": float(sg.get("total_elevation_gain") or 0.0),
                "dist_route_m": round(dm, 0),
                "climb": grade >= 3.0,
            })
    segs.sort(key=lambda x: x["km"])
    return segs, len(boxes)


# ── Appariement segments ↔ montées détectées ────────────────────────────
def match_climbs(segments, climbs):
    """Pour chaque montée détectée, les segments candidats qui la recouvrent
    (≥25 % de la montée, ou ≥60 % du segment inclus dans la montée).

    climbs : [(start_km, len_km, grad_pct)] ; segments : sortie collect_segments.
    Retourne {i_montée: [segment, ...]} trié par recouvrement décroissant."""
    out = {}
    for i, (km0, len_km, _g) in enumerate(climbs):
        km1 = km0 + len_km
        cands = []
        for sg in segments:
            s0, s1 = sg["km"], sg["km"] + sg["len_m"] / 1000.0
            ov = min(km1, s1) - max(km0, s0)
            sg_km = sg["len_m"] / 1000.0
            # Retenu si le segment couvre une bonne part de la montée, ou s'il
            # est lui-même largement inclus dedans (souvent le cas Strava).
            if ov > 0.25 * len_km or ov > 0.6 * sg_km:
                cands.append((-ov, sg["dist_route_m"], sg))
        cands.sort(key=lambda t: (t[0], t[1]))
        out[i] = [c[2] for c in cands]
    return out

