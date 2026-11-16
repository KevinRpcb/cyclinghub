import streamlit as st


def get_secret(key: str, default: str = "") -> str:
    """Lit un secret Streamlit sans jamais lever d'exception ni exposer la valeur."""
    try:
        val = st.secrets.get(key, None)
    except Exception:
        val = None
    if val is None or val == "":
        return default
    return str(val)


def openrouter_key() -> str:
    return get_secret("OPENROUTER_API_KEY")


def vekta_key() -> str:
    return get_secret("VEKTA_API_KEY")


def vekta_base_url() -> str:
    return get_secret("VEKTA_BASE_URL", "https://api.joinvekta.com")


def tp_username() -> str:
    return get_secret("TP_USERNAME")


def tp_password() -> str:
    return get_secret("TP_PASSWORD")


def pubmed_email() -> str:
    return get_secret("PUBMED_EMAIL", "coach.wt@equipe.com")


def pubmed_api_key() -> str:
    return get_secret("PUBMED_API_KEY", "")


def app_password() -> str:
    return get_secret("APP_PASSWORD")


WU_REQUIRED_FIELDS = (
    "duration", "z3blocks", "z3easy", "z3pctHR", "z3capPct",
    "z4reps", "z4work", "z4rec", "z4loPct", "z4hiPct", "easyAfterZ4",
    "z5reps", "z5work", "z5rec", "z5loPct", "z5hiPct", "sprintReps",
)


def wu_protocols():
    """Lit la table [WU_PROTOCOLS.*] des Secrets (les détails restent privés).

    Retourne (protocoles, erreur) : dict vide + message si absents/incomplets.
    """
    try:
        raw = st.secrets.get("WU_PROTOCOLS", None)
    except Exception:
        raw = None
    if not raw:
        return {}, "table [WU_PROTOCOLS] absente des Secrets Streamlit"
    protocols = {}
    for pid, cfg in raw.items():
        missing = [f for f in WU_REQUIRED_FIELDS if f not in cfg]
        if missing:
            return {}, (f"protocole '{pid}' incomplet dans les Secrets "
                        f"(champs manquants : {', '.join(missing)})")
        protocols[str(pid)] = {k: cfg[k] for k in cfg.keys()}
    return protocols, ""
