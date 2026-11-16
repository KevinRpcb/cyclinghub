import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.powermodel import fit_cp_model, rho_air, wprime_balance
from core.course import parse_course, build_segments, fetch_weather, simulate

st.set_page_config(page_title="Prévision de perf — WT Performance Hub", page_icon="🎯", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks"], key="prev_src")
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
                                key="prev_rider")
athlete = client.get_athlete(rider_id)
pp = client.get_power_profile(rider_id)

st.title("🎯 Prévision de performance — pacing & watts cibles")
st.caption("Glissez un fichier .gpx ou .fit du parcours : modèle aéro/gravité/roulement calibré "
           "sur les données du coureur, météo gratuite Open-Meteo, stratégie de pacing type BestBikeSplit.")

up = st.file_uploader("Parcours (.gpx / .fit)", type=["gpx", "fit"])
if not up:
    st.info("En attente d'un fichier de parcours.")
    st.stop()

try:
    points = parse_course(up.getvalue(), up.name)
    segs = build_segments(points)
except Exception as e:
    st.error(f"Fichier illisible : {e}")
    st.stop()

lats = [p["lat"] for p in points if p["lat"] is not None]
lons = [p["lon"] for p in points if p["lon"] is not None]
center = (float(np.mean(lats)), float(np.mean(lons))) if lats else None
total_km = segs["dist_m"].sum() / 1000.0
dplus = float(np.clip(segs["gradient_pct"], 0, None).mul(segs["dist_m"], axis=0).sum() / 1000.0)

c1, c2, c3 = st.columns(3)
c1.metric("Distance", f"{total_km:.1f} km")
c2.metric("D+", f"{dplus:.0f} m")
c3.metric("Segments", len(segs))

# ── Paramètres coureur ───────────────────────────────────────────────────
pts = [(s, pp[k]) for k, s in [("p1m", 60), ("p3m", 180), ("p5m", 300),
                               ("p10m", 600), ("p20m", 1200)] if pp.get(k)]
model = fit_cp_model(pts) or {"cp": float(pp["cp"]), "wprime": 20000.0}

with st.expander("⚙️ Paramètres de simulation", expanded=True):
    p1, p2, p3, p4 = st.columns(4)
    mass = p1.number_input("Masse totale (kg)", 40.0, 120.0, float(athlete.get("weight") or 70.0) + 8.0, 0.5)
    scx = p2.number_input("SCx (m²)", 0.15, 0.50, float(st.session_state.get(f"scx_{rider_id}", 0.22)), 0.005)
    cp = p3.number_input("CP (W)", 150, 700, int(model["cp"]))
    wprime = p4.number_input("W′ (kJ)", 5.0, 40.0, model["wprime"] / 1000.0, 0.5)
    p5, p6, p7 = st.columns(3)
    crr = p5.number_input("Crr", 0.002, 0.010, 0.004, 0.0005, format="%.4f")
    eta = p6.number_input("Rdt chaîne (%)", 94.0, 99.0, 97.6, 0.1)
    mode = p7.radio("Objectif", ["Puissance moyenne visée", "Temps objectif"], horizontal=True)

    w1, w2 = st.columns(2)
    use_weather = w1.checkbox("Utiliser la météo (Open-Meteo, gratuit)", value=center is not None)
    race_date = w2.date_input("Date de l'effort") if use_weather else None
    race_hour = st.slider("Heure de départ", 6, 20, 13) if use_weather else 13

weather = None
rho = rho_air(20.0, 1013.0)
wind_ms = wind_dir = 0.0
if use_weather and center:
    weather = fetch_weather(center[0], center[1], race_date.strftime("%Y-%m-%d"), race_hour)
    if "error" in weather:
        st.warning(weather["error"])
    else:
        rho = rho_air(weather["temp_c"], weather["pressure_hpa"])
        wind_ms, wind_dir = weather["wind_ms"], weather["wind_dir"]
        st.success(f"🌤️ Météo {weather['time']} : {weather['temp_c']:.0f} °C · vent "
                   f"{weather['wind_ms']:.1f} m/s ({weather['wind_dir']:.0f}°) · ρ = {rho:.3f} kg/m³")

goal_time_s = None
p_target = None
if mode.startswith("Temps"):
    h = st.number_input("Temps objectif (min)", 5.0, 300.0, max(5.0, total_km * 1.35), 0.5)
    goal_time_s = h * 60.0
else:
    p_target = st.number_input("Puissance moyenne visée (W)", 100, 700, int(cp * 0.95))

with st.spinner("Simulation du pacing…"):
    result = simulate(segs, mass, scx, rho, cp, wprime * 1000.0,
                      p_target=p_target, goal_time_s=goal_time_s, crr=crr,
                      eta=eta / 100.0, wind_ms=wind_ms, wind_dir=wind_dir)

out = result["df"]
tt = result["total_s"]
h, rem = divmod(tt, 3600)
mn, s = divmod(rem, 60)
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Temps estimé", f"{int(h):02d}:{int(mn):02d}:{int(s):02d}")
k2.metric("Puissance moyenne", f"{result['avg_w']:.0f} W", f"{result['avg_w']/mass:.1f} W/kg")
k3.metric("Normalisée (NP)", f"{result['np_w']:.0f} W")
k4.metric("IF", f"{result['np_w']/cp:.2f}")
k5.metric("W′ mini", f"{result['min_wprime_j']/1000:.1f} kJ",
          "⚠️ épuisée" if result["min_wprime_j"] <= 0 else "")

if result["min_wprime_j"] <= 0:
    st.error("La stratégie épuise W′ avant l'arrivée : augmentez la puissance cible de durée "
             "longue, réduisez les pics en côte ou allongez le temps objectif.")

fig = go.Figure()
fig.add_trace(go.Scatter(x=out["start_km"], y=out["power_w"], name="Puissance cible (W)",
                         line=dict(color="#e74c3c", width=2), yaxis="y2"))
fig.add_trace(go.Scatter(x=out["start_km"], y=out["speed_kmh"], name="Vitesse (km/h)",
                         line=dict(color="#2980b9", width=2)))
fig.add_trace(go.Scatter(x=out["start_km"], y=out["wprime_left_j"] / 1000.0, name="W′ restant (kJ)",
                         line=dict(color="#27ae60", width=2, dash="dot"), yaxis="y2"))
fig.update_layout(
    xaxis_title="Distance (km)", yaxis_title="Vitesse (km/h)", yaxis2=dict(overlaying="y", side="right", title="W / kJ"),
    height=420, template="plotly_white", hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="right", x=1))
st.plotly_chart(fig, use_container_width=True)

with st.expander("📋 Plan de pacing détaillé (par segment)"):
    st.dataframe(out[["start_km", "dist_m", "gradient_pct", "power_w", "speed_kmh", "time_s"]],
                 use_container_width=True, hide_index=True)
    csv = out.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Exporter le plan (CSV pour l'unité de compteur)", csv,
                       f"pacing_{rider_id}_{up.name}.csv", "text/csv")

st.caption("Modèle : P = ½·ρ·SCx·v²·v + m·g·(pente + Crr)·v, rendement chaîne inclus. "
           "Le pacing surpondère les sections en montée (sensibilité de la vitesse à la puissance) "
           "et sous-alimente les descentes — logique BestBikeSplit.")
