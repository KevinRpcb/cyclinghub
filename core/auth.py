import hmac

import streamlit as st

from .config import app_password


def _check():
    expected = app_password()
    entered = st.session_state.get("password", "")
    if not expected:
        st.session_state["auth_error"] = (
            "Aucun mot de passe configuré. Ajoutez APP_PASSWORD dans les Secrets Streamlit."
        )
        return
    if entered and hmac.compare_digest(entered, expected):
        st.session_state["auth"] = True
        st.session_state.pop("password", None)
        st.session_state.pop("pwd_error", None)
        st.session_state.pop("auth_error", None)
        st.rerun()
    else:
        st.session_state["pwd_error"] = True


def gate():
    """Verrou d'accès : comparaison à temps constant, aucune fuite d'information."""
    if st.session_state.get("auth"):
        return
    st.title("🔒 WT Performance Hub — Accès restreint")
    st.caption("Espace équipe World Tour. Authentification requise.")
    st.text_input("Mot de passe équipe :", type="password", on_change=_check, key="password")
    if st.session_state.get("pwd_error"):
        st.error("Mot de passe incorrect.")
    if st.session_state.get("auth_error"):
        st.error(st.session_state["auth_error"])
    st.stop()
