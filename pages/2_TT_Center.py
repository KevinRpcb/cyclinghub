from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.powermodel import (robust_cp_model, target_power, estimate_scx, rho_air,
                             quick_target, solve_speed)
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
_keys = list(labels.keys())
_shared = st.session_state.get("rider_id")
_idx = _keys.index(_shared) if _shared in _keys else 0
rider_id = st.sidebar.selectbox("Coureur", _keys, index=_idx, format_func=lambda x: labels[x],
                                key="tt_rider")
st.session_state["rider_id"] = rider_id
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
    model = robust_cp_model(pts, pp)
    if model:
        src_lbl = model.get("source", "efforts")
        if src_lbl == "efforts":
            st.success(f"CP = **{model['cp']:.0f} W** · W′ = **{model['wprime']/1000:.1f} kJ** "
                       f"(RMSE {model['rmse']:.0f} W sur {len(model['points'])} points)")
        else:
            st.info(f"Données insuffisantes pour un modèle CP fiable sur les efforts — "
                    f"courbe tracée à partir du **{src_lbl}**. "
                    f"Ajoute des efforts CLM maximaux pour l'affiner.")
        if model.get("excluded"):
            ex = ", ".join(f"{round(t/60)}′ @ {p} W" for t, p in model["excluded"])
            st.caption(f"Efforts écartés du modèle (non maximaux, ils tiraient la CP vers "
                       f"le bas) : {ex}.")
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

        st.markdown("**Convertisseur PPR** — puissance ↔ durée")
        cv1, cv2 = st.columns(2)
        with cv1:
            st.caption("Puissance → durée tenable")
            cp_w = st.number_input("Puissance (W)", 50, 1500, int(model["cp"] * 1.1),
                                   key="ppr_w")
            if cp_w <= model["cp"]:
                st.info(f"{cp_w} W est sous la CP ({model['cp']:.0f} W) : allure tenable "
                        f"très longtemps (effort d'endurance, pas d'épuisement du modèle).")
            else:
                t_hold = model["wprime"] / (cp_w - model["cp"])
                hh, rem = divmod(int(round(t_hold)), 3600)
                mm, ss = divmod(rem, 60)
                st.success(f"À **{cp_w} W**, le modèle tient **{hh}:{mm:02d}:{ss:02d}** "
                           f"(épuisement complet de W′).")
                if t_hold > 3600:
                    st.warning("Au-delà d'1 h, le modèle extrapole : valeur indicative.")
        with cv2:
            st.caption("Durée → puissance PPR")
            ch1, ch2, ch3 = st.columns(3)
            d_h = ch1.number_input("h", 0, 12, 0, key="ppr_dh")
            d_m = ch2.number_input("min", 0, 59, 30, key="ppr_dm")
            d_s = ch3.number_input("s", 0, 59, 0, key="ppr_ds")
            t_in = d_h * 3600 + d_m * 60 + d_s
            if t_in < 20:
                st.info("Entre une durée d'au moins 20 secondes.")
            else:
                p_hold = model["cp"] + model["wprime"] / t_in
                st.success(f"Sur **{d_h}\u2009h {d_m:02d}\u2009min {d_s:02d}\u2009s**, le PPR "
                           f"donne **{p_hold:.0f} W**.")
                if t_in > 3600:
                    st.warning("Au-delà d'1 h, le modèle extrapole : valeur indicative.")
    else:
        st.warning("Aucune donnée puissance pour ce coureur (ni profil, ni effort). "
                   "Ajoute un effort manuellement dans l'onglet ci-dessus ou change de source.")

    with st.expander("🔍 Diagnostic données — montre-moi ce bloc si la courbe manque"):
        st.write(f"Source : **{type(client).__name__}**"
                 + (f" — {client.last_error}" if getattr(client, "last_error", None) else ""))
        st.write(f"Efforts CLM trouvés : **{len(efforts)}** · saisis à la main : **{len(manual)}**")
        st.write(f"Profil puissance renvoyé par l'API : **{pp if pp else 'VIDE'}**")
        if model:
            st.write(f"Modèle CP tracé : source **{model.get('source', 'efforts')}**, "
                     f"CP {model['cp']:.0f} W, W′ {model['wprime'] / 1000:.1f} kJ, "
                     f"{len(model['points'])} points.")
        else:
            st.write("Modèle CP : **AUCUN** — aucune donnée puissance du tout pour ce coureur.")

with t2:
    st.subheader("Estimation du SCx (S·Cx ≈ CdA) du coureur")
    st.caption("À partir d'un effort stable : puissance, vitesse, pente, conditions. "
               "Méthode inverse du modèle aéro/roulement/gravité.")
    base_rho = rho_air(20.0, 1013.0)
    sc1, sc2, sc3, sc4 = st.columns(4)
    s_power = sc1.number_input("Puissance (W)", 100, 1500,
                               int(pp.get("p20m") or pp.get("cp") or 300))
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
    st.subheader("Cible rapide — durée ou distance")
    st.caption("Puissance cible issue du PPR (modèle CP). Choisis une durée OU une distance "
               "de course : la puissance et le temps s'équilibrent automatiquement.")
    if model:
        q_mode = st.radio("Mode", ["Durée cible", "Distance cible"], horizontal=True,
                          key="qt_mode")
        qt1, qt2, qt3 = st.columns(3)
        q_grad = qt2.number_input("Pente du parcours (%)", -5.0, 15.0, 0.0, step=0.1)
        q_wind = qt3.number_input("Vent de face (m/s, négatif = dos)", -10.0, 15.0, 0.0,
                                  step=0.5)
        scx_default = st.session_state.get(f"scx_{rider_id}", 0.22)
        q_scx = st.number_input("SCx (m²)", 0.15, 0.50, float(scx_default), step=0.005)
        rho_q = rho_air(s_temp, s_press)
        q_mass = float(athlete.get("weight") or 70.0) + 8.0

        t_eff = None
        q_dur = None
        q_dist = None
        if q_mode == "Durée cible":
            q_dur = qt1.number_input("Durée cible (min)", 1.0, 90.0, 20.0, step=0.5)
            t_eff = q_dur * 60.0
        else:
            q_dist = qt1.number_input("Distance cible (km)", 1.0, 100.0, 20.0, step=0.1)
            t_eff = 1200.0
            for _ in range(40):
                w_it = target_power(model, t_eff)
                v_it = solve_speed(w_it, q_grad, q_mass, q_scx, rho_q,
                                   headwind_ms=q_wind)
                t_new = q_dist * 1000.0 / max(v_it, 0.5)
                if abs(t_new - t_eff) < 0.2:
                    t_eff = t_new
                    break
                t_eff = 0.5 * (t_eff + t_new)

        res = quick_target(model, t_eff, q_mass, q_scx, rho_q,
                           gradient_pct=q_grad, headwind_ms=q_wind)
        a, b, c, d = st.columns(4)
        a.metric("Puissance cible", f"{res['watts']} W", f"{res['wkg']} W/kg")
        b.metric("Vitesse estimée", f"{res['speed_kmh']} km/h")
        if q_mode == "Distance cible":
            th, tr = divmod(int(round(t_eff)), 3600)
            tm, ts = divmod(tr, 60)
            b.metric("Temps estimé", f"{th}:{tm:02d}:{ts:02d}")
        c.metric("CP de référence", f"{model['cp']:.0f} W")
        d.metric("W′ disponible", f"{model['wprime']/1000:.1f} kJ")

        if q_mode == "Durée cible":
            st.info(f"Pour **{q_dur:.0f} min** sur pente {q_grad:+.1f} % : viser "
                    f"**{res['watts']} W** (≈ {res['wkg']} W/kg). En course, partir à 97 % "
                    f"de cette cible les 2 premières minutes puis converger vers 100 %.")
        else:
            st.info(f"Pour **{q_dist:.1f} km** sur pente {q_grad:+.1f} % : viser "
                    f"**{res['watts']} W** (≈ {res['wkg']} W/kg), soit un temps estimé de "
                    f"**{th}:{tm:02d}:{ts:02d}** à {res['speed_kmh']} km/h.")

        durs_min = [1, 3, 5, 10, 20, 60]
        t_min = t_eff / 60.0
        d_near = min(durs_min, key=lambda d: abs(d - t_min))
        st.caption("Repères PPR : ce que le coureur peut tenir à ces durées standard. "
                   "Ta cible est surlignée en dessous.")
        ref = st.columns(len(durs_min))
        for col, dm in zip(ref, durs_min):
            lbl = f"✅ {dm} min" if dm == d_near else f"{dm} min"
            col.metric(lbl, f"{target_power(model, dm * 60):.0f} W")
        d_lo = min(p[0] for p in model["points"]) / 60.0
        d_hi = max(p[0] for p in model["points"]) / 60.0
        if t_min < d_lo * 0.5 or t_min > d_hi * 2.0:
            st.warning(f"Extrapolation peu fiable : le modèle est ajusté sur des efforts de "
                       f"{d_lo:.1f} à {d_hi:.1f} min, tu demandes {t_min:.1f} min. "
                       f"Ajoute un effort proche de cette durée pour affiner le PPR.")
    else:
        st.warning("Ajustez d'abord le modèle CP (onglet Suivi).")
