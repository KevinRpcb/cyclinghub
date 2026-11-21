import io

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.powermodel import rho_air
from core.course import parse_course, build_segments, parse_fit_activity, fetch_weather
from core.ttt import (DRAFT_GAPS, DEFAULT_GAP, team_simulation, best_order,
                      analyze_ttt_file, draft_factor)

st.set_page_config(page_title="Mode TTT — WT Performance Hub", page_icon="🏁", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks"], key="ttt_src")
client = VektaClient()
if src == "TrainingPeaks":
    tp = session_client()
    if not tp.configured:
        st.warning("TrainingPeaks : ajoute TP_USERNAME et TP_PASSWORD dans les Secrets "
                   "Streamlit — Vekta est utilisé en attendant.")
    else:
        riders_tp = tp.get_athletes()
        if riders_tp:
            client = tp
        else:
            st.warning(f"TrainingPeaks : {tp.last_error} — repli sur Vekta.")
riders = client.get_athletes()

st.title("🏁 Mode TTT — vitesse du groupe, rotations & abri")
st.caption("Modèle basé sur les études de drafting (Blocken 2018, Barry 2016, "
           "Kyle & Burke 1979). La vitesse du groupe est maintenue : le modèle "
           "cherche l'ordre de rotation le plus rapide.")

t_sim, t_sci, t_post = st.tabs(["🏁 Simulateur", "🔬 Base scientifique", "📥 Analyse post-course"])


def _profile_to_rider(a):
    aid = a["id"]
    pp = {}
    try:
        pp = client.get_power_profile(aid) or {}
    except Exception:
        pass
    cp = pp.get("cp") or pp.get("p60m") or 350.0
    p1 = pp.get("p1m") or cp * 1.5
    wprime = max((p1 - cp) * 60.0, 8000.0) if p1 > cp else 20000.0
    w = a.get("weight") or 70.0
    try:
        w = float(w)
    except (TypeError, ValueError):
        w = 70.0
    return {"name": a.get("name", "?"), "cp": round(float(cp)), "wprime": round(wprime),
            "weight": w, "scx": 0.21}


with t_sim:
    st.subheader("1 · Parcours")
    up = st.file_uploader("Glisse le parcours du TTT (.gpx / .fit)", type=["gpx", "fit"],
                          key="ttt_course")
    segs = None
    if up is not None:
        try:
            pts = parse_course(up.getvalue(), up.name)
            segs = build_segments(pts)
            total_km = segs["dist_m"].sum() / 1000.0
            dplus = float((np.clip(segs["gradient_pct"], 0, None) / 100.0)
                          .mul(segs["dist_m"]).sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Distance", f"{total_km:.1f} km")
            c2.metric("D+", f"{dplus:.0f} m")
            c3.metric("Segments", len(segs))
        except Exception as e:
            st.error(f"Parcours illisible : {type(e).__name__} — {e}")

    st.subheader("2 · Coureurs (2 à 9)")
    default_rows = [_profile_to_rider(a) for a in riders[:6]] or [
        {"name": "Coureur 1", "cp": 420, "wprime": 22000, "weight": 72.0, "scx": 0.21}]
    riders_df = st.data_editor(pd.DataFrame(default_rows),
                               num_rows="dynamic", use_container_width=True,
                               key="ttt_riders",
                               column_config={
                                   "name": st.column_config.TextColumn("Nom"),
                                   "cp": st.column_config.NumberColumn("CP (W)", min_value=150, max_value=700),
                                   "wprime": st.column_config.NumberColumn("W' (J)", min_value=4000, max_value=40000),
                                   "weight": st.column_config.NumberColumn("Poids (kg)", min_value=45, max_value=100),
                                   "scx": st.column_config.NumberColumn("SCx", min_value=0.15, max_value=0.35, step=0.005),
                               })
    team = riders_df.dropna(subset=["name"]).to_dict("records")
    team = [r for r in team if r.get("name")]

    st.subheader("3 · Rotation & conditions")
    cA, cB, cC = st.columns(3)
    pull_s = cA.slider("Durée d'un relais (s)", 10, 60, 25)
    gap = cB.selectbox("Écart dans la ligne", list(DRAFT_GAPS.keys()), index=1)
    n_r = st.session_state.get("_n_riders", len(team))
    st.session_state["_n_riders"] = len(team)
    cC.metric("Coureurs", len(team))
    cD, cE, cF = st.columns(3)
    wind_ms = cD.slider("Vent (m/s)", 0.0, 15.0, 0.0, 0.5)
    wind_dir = cE.slider("Direction du vent (°, 0 = N)", 0, 355, 0, 5)
    rho = cF.number_input("Densité de l'air ρ", value=1.225, step=0.005, format="%.3f")

    if st.button("🏁 Lancer la simulation", type="primary", disabled=segs is None or len(team) < 2):
        with st.spinner("Simulation en cours…"):
            sim = team_simulation(segs, team, pull_s=pull_s, gap=gap, rho=rho,
                                  wind_ms=wind_ms, wind_dir=wind_dir)
        st.session_state["ttt_sim"] = sim
        st.session_state["ttt_team"] = team
        st.session_state["ttt_segs"] = segs
        st.session_state["ttt_params"] = (pull_s, gap, rho, wind_ms, wind_dir)

    sim = st.session_state.get("ttt_sim")
    if sim:
        team = st.session_state["ttt_team"]
        segs = st.session_state["ttt_segs"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Temps total", f"{int(sim['total_s'] // 60)}'{int(sim['total_s'] % 60):02d}\"")
        m2.metric("Vitesse moyenne", f"{sim['avg_kmh']} km/h")
        m3.metric("Puissances solo (CP→effort)", ", ".join(str(p) for p in sim["p_solo"]) + " W")

        st.markdown("**Le même ordre, testé dans tous les sens :**")
        if st.button("🔎 Calculer le meilleur ordre"):
            with st.spinner("Recherche du meilleur ordre…"):
                _pp = st.session_state["ttt_params"]
                bo = best_order(segs, team, pull_s=_pp[0], gap=_pp[1], rho=_pp[2],
                                wind_ms=_pp[3], wind_dir=_pp[4])
            st.session_state["ttt_bo"] = bo
        bo = st.session_state.get("ttt_bo")
        if bo:
            names = [team[i]["name"] for i in bo["order"]]
            gain = bo["base_total_s"] - bo["total_s"]
            st.success(f"Ordre recommandé : **{' → '.join(names)}** "
                       f"({int(bo['total_s'] // 60)}'{int(bo['total_s'] % 60):02d}\""
                       f"{f', gain {gain:.0f} s' if gain > 0.5 else ', ordre actuel déjà optimal'})")
            for c in bo["candidates"][1:]:
                cn = [team[i]["name"] for i in c["order"]]
                st.caption(f"Alternative : {' → '.join(cn)} ({int(c['total_s'] // 60)}'{int(c['total_s'] % 60):02d}\")")

        st.markdown("**Chaque coureur pendant la course :**")
        per = pd.DataFrame(sim["per_rider"])
        per = per[["name", "pct_front", "pulls", "avg_front_w", "avg_draft_w", "avg_race_w"]]
        per.columns = ["Coureur", "% temps devant", "Relais", "W moyen devant",
                       "W moyen à l'abri", "W moyen sur la course"]
        st.dataframe(per, use_container_width=True)

        st.markdown("**Timeline des relais :**")
        fig = go.Figure()
        colors = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2",
                  "#be185d", "#65a30d", "#57534e"]
        name_list = [r["name"] for r in team]
        for k, p in enumerate(sim["pulls"]):
            nm = name_list[p["rider"]] if p["rider"] < len(name_list) else str(p["rider"])
            fig.add_trace(go.Bar(
                x=[p["t1"] - p["t0"]], base=[p["t0"]], y=[nm], orientation="h",
                marker_color=colors[p["rider"] % len(colors)],
                name=nm, showlegend=k == 0,
                hovertemplate=f"{nm} : {p['t0']:.0f}→{p['t1']:.0f} s<extra></extra>"))
        fig.update_layout(barmode="stack", height=60 + 34 * len(name_list),
                          template="plotly_white", showlegend=False,
                          xaxis_title="Temps (s)", yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig, use_container_width=True)

with t_sci:
    st.subheader("Ce que dit la science sur l'abri dans un TTT")
    st.markdown("""
Dans un contre-la-montre par équipe, celui qui roule derrière dépense **beaucoup
moins** que celui qui est devant. La fraction de la résistance de l'air conservée
dépend de l'écart roue-roue et de la position dans la ligne :

| Position | Roue dans la roue (~0,3 m) | Mi-roue (~0,5 m) | Une roue (~1 m) |
|---|---|---|---|
| 1 (devant) | 100 % | 100 % | 100 % |
| 2 | 62 % | 70 % | 78 % |
| 3 | 55 % | 62 % | 70 % |
| 4 | 52 % | 58 % | 66 % |
| 5+ | ~50 % | ~55 % | ~63 % |

**Lecture :** en mi-roue, le 2e rouleur ne fournit que ~70 % de la puissance du
chef de file pour tenir la même vitesse. Plus la ligne est serrée, plus l'équipe
gagne — c'est pourquoi **la vitesse du groupe se maintient mieux avec des relais
courts et une ligne serrée**.

**Références :**
- Blocken et al. (2018, TU Eindhoven) — aérodynamique du TTT, positions en ligne,
  écart roue-roue ;
- Barry et al. (2016) — réduction de traînée derrière 1 à n coureurs ;
- Kyle & Burke (1979) — drafting roue dans la roue (−30 à −45 %).
""")
    st.caption("Le simulateur utilise exactement ces facteurs : chaque coureur "
               "derrière doit tenir `puissance solo × facteur de sa position`.")

with t_post:
    st.subheader("Analyse d'un fichier post-course (coureur isolé)")
    st.caption("Glisse le .fit d'un coureur après le TTT : le modèle devine sa "
               "position à chaque instant (devant / abri) et conclut sur la rotation.")
    upf = st.file_uploader("Fichier .fit de la course", type=["fit"], key="ttt_fit")
    c1, c2 = st.columns(2)
    mass = c1.number_input("Poids coureur + vélo (kg)", value=80.0, step=0.5)
    scx_est = c2.number_input("SCx du coureur", value=0.21, step=0.005, format="%.3f")
    if upf is not None and st.button("📥 Analyser la course", type="primary"):
        try:
            recs = parse_fit_activity(upf.getvalue())
            res = analyze_ttt_file(recs, mass_kg=float(mass), scx=float(scx_est),
                                   rho=1.225)
        except Exception as e:
            res = None
            st.error(f"Fichier illisible : {type(e).__name__} — {e}")
        if res:
            st.session_state["ttt_post"] = res
        elif res is None and "ttt_post" not in st.session_state:
            st.warning("Pas assez de données puissance dans ce fichier "
                       "(il faut puissance + distance, au moins 20 secondes).")

    res = st.session_state.get("ttt_post")
    if res:
        d = res["df"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Temps devant", f"{res['front_pct']} %")
        m2.metric("Relais", len(res["stints"]))
        m3.metric("Rapport d'abri", f"{res['avg_ratio_back']:.2f}")
        m4.metric("W devant / abri", f"{res['avg_front_w']:.0f} / {res['avg_back_w']:.0f}")

        pos_lbl = {1: "Devant", 2: "2e", 3: "3e", 4: "4e", 5: "5e", 6: "6e+", 0: "Non estimé"}
        pct = res["pct_pos"]
        st.bar_chart(x=[pos_lbl[k] for k in sorted(pct)],
                     y=[pct[k] for k in sorted(pct)],
                     height=240, use_container_width=True)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=d.index, y=d["power"], mode="lines",
                                 name="Puissance (W)", line=dict(color="#dc2626", width=1.5)))
        fig.add_trace(go.Scatter(x=d.index, y=d["position"], mode="lines",
                                 name="Position estimée", line=dict(color="#2563eb", width=1),
                                 yaxis="y2", line_shape="hv"))
        fig.update_layout(template="plotly_white", height=380, hovermode="x unified",
                          yaxis=dict(title="Puissance (W)"),
                          yaxis2=dict(title="Position", overlaying="y", side="right",
                                      range=[0.5, 6.5], dtick=1))
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("**Conclusions :**")
        for c in res["conclusions"]:
            st.markdown(f"- {c}")
        with st.expander("Détail des relais devant"):
            st.dataframe(pd.DataFrame(
                [{"Début (s)": s[0], "Fin (s)": s[1], "Durée (s)": s[1] - s[0] + 1}
                 for s in res["stints"]]), use_container_width=True)
