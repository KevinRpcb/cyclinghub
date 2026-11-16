from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.powermodel import fit_cp_model, target_power, estimate_scx, rho_air, quick_target
from core.config import get_secret

st.set_page_config(page_title="TT Center — WT Performance Hub", page_icon="⏱️", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks"], key="tt_src")
client = VektaClient()
if src == "TrainingPeaks":
    tp = session_client()
    if not tp.configured:
        st.warning("TrainingPeaks : ajoute TP_USERNAME et TP_PASSWORD dans les Secrets "
                   "Streamlit — Vekta est utilisée en attendant.")
    else:
        riders_tp = tp.get_athletes()
        if riders_tp:
            client = tp
        else:
            st.warning(f"TrainingPeaks : {tp.last_error} — repli sur Vekta.")
riders = client.get_athletes()
labels = {a["id"]: f"{a.get('name','?')} — {a.get('role','')}" for a in riders}
rider_id = st.sidebar.selectbox("Coureur", list(labels.keys()), format_func=lambda x: labels[x],
                                key="tt_rider")
athlete = client.get_athlete(rider_id)
pp = client.get_power_profile(rider_id)

st.title("⏱️ TT Center — Suivi CLM, PPR, SCx & cibles")

# ── Efforts CLM (Vekta + saisie manuelle, session) ───────────────────────
efforts = list(client.get_tt_efforts(rider_id))
if isinstance(client, TPClient) and not efforts:
    st.caption("Efforts CLM : aucune séance vélo avec puissance trouvée dans "
               "TrainingPeaks (120 derniers jours). Les efforts saisis à la main "
               "sont toujours possibles.")
manual = st.session_state.setdefault("tt_manual", [])
efforts_all = efforts + manual

t1, t2, t3 = st.tabs(["📈 Suivi des efforts & PPR", "🧮 Estimation SCx", "🎯 Cible rapide"])

with t1:
    st.subheader("Efforts sur le vélo CLM — course & entraînement")
    df = pd.DataFrame(efforts_all)
    if not df.empty:
        for col in ("np_w", "speed_kmh", "gradient_pct", "type"):
            if col not in df.columns:
                df[col] = np.nan
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date", ascending=False)
        show = df.copy()
        show["IF"] = (show["avg_w"] / pp["cp"]).round(2)
        show["W/kg"] = (show["avg_w"] / max(float(athlete.get("weight", 0) or 0), 1)).round(1)
        show["dur_min"] = (show["duration_s"] / 60).round(1)
        st.dataframe(show[["date", "type", "dur_min", "avg_w", "np_w", "IF", "W/kg",
                           "speed_kmh", "gradient_pct"]].fillna("n/c"),
                     use_container_width=True, hide_index=True)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["date"], y=df["avg_w"], mode="lines+markers",
                                 name="Puissance moyenne", line=dict(color="#2980b9", width=2)))
        fig.add_trace(go.Scatter(x=df["date"], y=df["np_w"], mode="markers", name="NP",
                                 marker_color="#e67e22"))
        fig.update_layout(height=300, template="plotly_white", margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Aucun effort enregistré.")

    with st.expander("➕ Ajouter un effort manuellement"):
        with st.form("add_effort", clear_on_submit=True):
            ec1, ec2, ec3, ec4 = st.columns(4)
            e_date = ec1.date_input("Date", datetime.today())
            e_type = ec2.selectbox("Type", ["course", "entrainement"])
            e_dur = ec3.number_input("Durée (min)", 1.0, 180.0, 20.0, step=0.5)
            e_w = ec4.number_input("Puissance moy. (W)", 50, 1500, int(pp["cp"]))
            ec5, ec6, ec7 = st.columns(3)
            e_np = ec5.number_input("NP (W)", 50, 1600, int(pp["cp"] * 1.03))
            e_speed = ec6.number_input("Vitesse (km/h)", 10.0, 70.0, 47.0, step=0.1)
            e_grad = ec7.number_input("Pente moy. (%)", -5.0, 15.0, 0.0, step=0.1)
            if st.form_submit_button("Enregistrer"):
                manual.append({"date": e_date.strftime("%Y-%m-%d"), "type": e_type,
                               "duration_s": int(e_dur * 60), "avg_w": int(e_w), "np_w": int(e_np),
                               "speed_kmh": float(e_speed), "gradient_pct": float(e_grad)})
                st.rerun()

    st.subheader("PPR — modèle Critique Puissance ajusté sur les efforts")
    pts = [(e["duration_s"], e["avg_w"]) for e in efforts_all]
    pts += [(s, pp[k]) for k, s in [("p1m", 60), ("p3m", 180), ("p5m", 300),
                                    ("p10m", 600), ("p20m", 1200)] if pp.get(k)]
    model = fit_cp_model(pts)
    if model:
        st.success(f"CP = **{model['cp']:.0f} W** · W′ = **{model['wprime']/1000:.1f} kJ** "
                   f"(RMSE {model['rmse']:.0f} W sur {len(model['points'])} points)")
        t_fit = np.logspace(np.log10(20), np.log10(3600), 60)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[p[0] for p in model["points"]],
                                 y=[p[1] for p in model["points"]], mode="markers",
                                 name="Meilleurs efforts", marker=dict(size=9, color="#e74c3c")))
        fig.add_trace(go.Scatter(x=t_fit, y=[target_power(model, t) for t in t_fit],
                                 mode="lines", name=f"Modèle CP", line=dict(color="#2980b9", width=3)))
        fig.update_xaxes(type="log", title="Durée (s, log)")
        fig.update_yaxes(title="Puissance (W)")
        fig.update_layout(height=340, template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("Pas assez d'efforts (>20 s) pour ajuster le modèle CP.")

with t2:
    st.subheader("Estimation du SCx (S·Cx ≈ CdA) du coureur")
    st.caption("À partir d'un effort stable : puissance, vitesse, pente, conditions. "
               "Méthode inverse du modèle aéro/roulement/gravité.")
    base_rho = rho_air(20.0, 1013.0)
    sc1, sc2, sc3, sc4 = st.columns(4)
    s_power = sc1.number_input("Puissance (W)", 100, 1500, int(pp["p20m"] or pp["cp"]))
    s_speed = sc2.number_input("Vitesse (km/h)", 10.0, 70.0, 47.0, step=0.1)
    s_grad = sc3.number_input("Pente (%)", -5.0, 15.0, 0.0, step=0.1)
    s_mass = sc4.number_input("Masse totale (kg)", 40.0, 120.0,
                              float(athlete.get("weight") or 70.0) + 8.0, step=0.5)
    sc5, sc6, sc7 = st.columns(3)
    s_temp = sc5.number_input("Température (°C)", -10.0, 45.0, 20.0, step=1.0)
    s_press = sc6.number_input("Pression (hPa)", 900.0, 1080.0, 1013.0, step=1.0)
    s_crr = sc7.number_input("Crr", 0.002, 0.010, 0.004, step=0.0005, format="%.4f")
    rho = rho_air(s_temp, s_press)
    scx = estimate_scx(s_power, s_speed, s_grad, s_mass, rho, s_crr)
    if scx == scx and 0.12 <= scx <= 0.60:
        st.success(f"**SCx ≈ {scx:.3f} m²** (ρ = {rho:.3f} kg/m³) — cohérent pour un CLM.")
        st.session_state[f"scx_{rider_id}"] = round(scx, 3)
    else:
        st.error(f"Valeur hors domaine physique ({scx:.3f} m²) — vérifiez vitesse/pente/conditions.")
    st.caption("Astuce : moyennez sur ≥ 5 min à puissance stable, sans vent, pente < 1 %.")

with t3:
    st.subheader("Cible rapide — durée spéciale")
    st.caption("Donne la puissance cible issue du PPR (modèle CP) et l'allure associée.")
    if model:
        qt1, qt2 = st.columns(2)
        q_dur = qt1.number_input("Durée cible (min)", 1.0, 90.0, 20.0, step=0.5)
        q_grad = qt2.number_input("Pente du parcours (%)", -5.0, 15.0, 0.0, step=0.1)
        scx_default = st.session_state.get(f"scx_{rider_id}", 0.22)
        q_scx = st.number_input("SCx (m²)", 0.15, 0.50, float(scx_default), step=0.005)
        rho_q = rho_air(s_temp, s_press)
        res = quick_target(model, q_dur * 60, float(athlete.get("weight") or 70.0),
                           q_scx, rho_q,
                           gradient_pct=q_grad)
        a, b, c, d = st.columns(4)
        a.metric("Puissance cible", f"{res['watts']} W", f"{res['wkg']} W/kg")
        b.metric("Vitesse estimée", f"{res['speed_kmh']} km/h")
        c.metric("CP de référence", f"{model['cp']:.0f} W")
        d.metric("W′ disponible", f"{model['wprime']/1000:.1f} kJ")
        st.info(f"Pour **{q_dur:.0f} min** sur pente {q_grad:+.1f} % : viser **{res['watts']} W** "
                f"(≈ {res['wkg']} W/kg). En course, partir à 97 % de cette cible les 2 premières "
                f"minutes puis converger vers 100 %.")
    else:
        st.warning("Ajustez d'abord le modèle CP (onglet Suivi).")
