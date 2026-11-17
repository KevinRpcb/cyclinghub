"""Sauvegarde chiffrée du plannif dans un Gist GitHub privé.

Le contenu est chiffré avant l'envoi : même si quelqu'un trouve le fichier,
il est illisible sans la clé. La clé vient des Secrets Streamlit
(PLAN_KEY, ou dérivée du mot de passe de l'app si PLAN_KEY est absent)
et ne quitte jamais l'application.
"""
import base64
import hashlib
import hmac
import json
import os

import requests
import streamlit as st

from core.config import get_secret

GIST_FILE = "plannif.wt.enc"
GIST_DESC = "wt-performance-hub-plan"
API = "https://api.github.com"


def plan_key() -> bytes:
    custom = get_secret("PLAN_KEY")
    if custom:
        return hashlib.sha256(("plan:" + custom).encode()).digest()
    app_pwd = get_secret("APP_PASSWORD", "wt-performance-hub")
    return hashlib.sha256(("plan:" + app_pwd).encode()).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def encrypt_json(data: dict, key: bytes) -> str:
    nonce = os.urandom(16)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(raw, _keystream(key, nonce, len(raw))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt_json(blob: str, key: bytes):
    try:
        data = base64.urlsafe_b64decode(blob.encode("ascii"))
        nonce, ct, tag = data[:16], data[16:-32], data[-32:]
        good = hmac.compare_digest(tag, hmac.new(key, nonce + ct, hashlib.sha256).digest())
        if not good:
            return None
        raw = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _headers():
    return {"Authorization": f"Bearer {get_secret('GIST_TOKEN')}",
            "Accept": "application/vnd.github+json"}


def find_gist():
    """Retrouve le gist du plan parmi les gists du token (pas besoin d'ID)."""
    try:
        res = requests.get(f"{API}/gists?per_page=100", headers=_headers(), timeout=15)
        if res.status_code != 200:
            return None
        for g in res.json():
            if GIST_FILE in (g.get("files") or {}):
                return g["id"]
    except Exception:
        pass
    return None


def save_plan(data: dict):
    if not get_secret("GIST_TOKEN"):
        return False, "Ajoute GIST_TOKEN dans les Secrets Streamlit (token GitHub avec le droit gist)."
    payload = {"description": GIST_DESC, "public": False,
               "files": {GIST_FILE: {"content": encrypt_json(data, plan_key())}}}
    try:
        gid = find_gist()
        if gid:
            res = requests.patch(f"{API}/gists/{gid}", headers=_headers(),
                                 json=payload, timeout=15)
        else:
            res = requests.post(f"{API}/gists", headers=_headers(), json=payload, timeout=15)
        if res.status_code in (200, 201):
            return True, "Plan enregistré (chiffré)."
        return False, f"GitHub a refusé (statut {res.status_code})."
    except Exception as e:
        return False, f"{type(e).__name__} — {e}"


def load_plan():
    if not get_secret("GIST_TOKEN"):
        return None, "Pas de GIST_TOKEN dans les Secrets."
    gid = find_gist()
    if not gid:
        return None, "Aucune sauvegarde trouvée (normal au premier usage)."
    try:
        res = requests.get(f"{API}/gists/{gid}", headers=_headers(), timeout=15)
        if res.status_code != 200:
            return None, f"GitHub a refusé (statut {res.status_code})."
        content = res.json()["files"][GIST_FILE]["content"]
        data = decrypt_json(content, plan_key())
        if data is None:
            return None, "Sauvegarde illisible (clé différente de celle du chiffrement)."
        return data, "Plan chargé depuis le cloud privé."
    except Exception as e:
        return None, f"{type(e).__name__} — {e}"
