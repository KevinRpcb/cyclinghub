"""Stockage du jeton Strava dans le Gist GitHub privé de l'équipe.

Pourquoi : à chaque rafraîchissement, Strava émet un NOUVEAU refresh token
et invalide l'ancien. S'il n'est pas sauvegardé, tout redémarrage de
l'application (Streamlit Cloud recycle le processus) repart avec un jeton
mort — d'où les erreurs HTTP 401 persistantes. On conserve donc le jeton
frais, chiffré (même mécanisme que le Plannif), dans le Gist privé ;
le jeton des Secrets sert uniquement de valeur de secours initiale.
"""
import requests

from core.config import get_secret
from core.planstore import API, GIST_DESC, _headers, encrypt_json, plan_key

FILE = "wt.strava.tokens.enc"
_DESC = GIST_DESC + " strava"


def _gist_id():
    try:
        res = requests.get(f"{API}/gists?per_page=100", headers=_headers(),
                           timeout=15)
        if res.status_code != 200:
            return None
        for g in res.json():
            if FILE in (g.get("files") or {}):
                return g["id"]
    except Exception:
        pass
    return None


def save_token(refresh_token: str, expires_at: float = 0.0) -> bool:
    """Sauvegarde chiffrée du refresh token frais (échec silencieux :
    la connexion marche quand même tant que le process vit)."""
    if not (get_secret("GIST_TOKEN") and refresh_token):
        return False
    payload = {"description": _DESC, "public": False,
               "files": {FILE: {"content": encrypt_json(
                   {"refresh_token": refresh_token, "expires_at": expires_at},
                   plan_key())}}}
    try:
        gid = _gist_id()
        url = f"{API}/gists/{gid}" if gid else f"{API}/gists"
        res = requests.patch(url, headers=_headers(), json=payload,
                             timeout=15) if gid else \
            requests.post(url, headers=_headers(), json=payload, timeout=15)
        return res.status_code in (200, 201)
    except Exception:
        return False


def load_token() -> dict:
    """{refresh_token, expires_at} ou {} si rien de sauvegardé."""
    if not get_secret("GIST_TOKEN"):
        return {}
    gid = _gist_id()
    if not gid:
        return {}
    try:
        res = requests.get(f"{API}/gists/{gid}", headers=_headers(), timeout=15)
        if res.status_code != 200:
            return {}
        blob = (res.json().get("files") or {}).get(FILE, {}).get("content", "")
        from core.planstore import decrypt_json
        d = decrypt_json(blob, plan_key()) if blob else None
        return d if isinstance(d, dict) and d.get("refresh_token") else {}
    except Exception:
        return {}


def clear_token() -> None:
    """Retire le jeton sauvegardé (après une nouvelle autorisation manuelle,
    pour repartir proprement)."""
    try:
        gid = _gist_id()
        if gid:
            requests.patch(f"{API}/gists/{gid}", headers=_headers(),
                           json={"files": {FILE: None}}, timeout=15)
    except Exception:
        pass
