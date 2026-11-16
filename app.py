import numpy as np
import pandas as pd
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.physiology import readiness, vfc_verdict

st.set_page_config(page_title="WT Performance Hub", page_icon="🚴", layout="wide",
                   initial_sidebar_state="expanded")
gate()

st.title("🚴 WT Performance Hub — Vue d'équipe")

client = VektaClient()
if client.last_error:
    st.warning(f"API Vekta injoignable ({client.last_error}) — données de démonstration affichées.")
riders = client.get_athletes()

rows, alerts = [], []
for a in riders:
    m = client.get_metrics(a["id"])
    score, comps = readiness(m)
    tsb = float(m.get("ctl", 0)) - float(m.get("atl", 0))
    rows.append({
        "Coureur": a.get("name"), "Profil": a.get("role"),
        "Readiness": score, "VFC (ms)": m.get("vfc_last"), "Baseline": m.get("vfc_baseline"),
        "Découplage (%)": m.get("decouplage_cardio"), "Sommeil": m.get("sleep_score"),
        "Charge 7j": m.get("load_7d"), "CTL": m.get("ctl"), "ATL": m.get("atl"), "TSB": round(tsb, 1),
    })
    if score < 45:
        alerts.append(f"🚨 {a.get('name')} — readiness {score}/100 : {vfc_verdict(m)}")
    elif tsb < -25:
        alerts.append(f"⚠️ {a.get('name')} — TSB {tsb:.0f} : fatigue accumulée élevée.")

if alerts:
    for al in alerts:
        st.error(al)
else:
    st.success("Aucun signal d'alerte sur l'équipe.")

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.markdown("---")
c1, c2, c3, c4 = st.columns(4)
c1.page_link("pages/1_fiche_coureur.py", label="🧑‍⚕️ Fiche coureur", icon="🧑‍⚕️")
c2.page_link("pages/2_tt_center.py", label="⏱️ TT Center", icon="⏱️")
c3.page_link("pages/3_prevision_perf.py", label="🎯 Prévision de perf", icon="🎯")
c4.page_link("pages/5_labo.py", label="🔬 Labo scientifique", icon="🔬")

st.caption("Sécurité : accès par mot de passe (comparaison à temps constant), clés uniquement dans "
           "les Secrets Streamlit, appels réseau avec timeout, fichiers uploadés validés.")
