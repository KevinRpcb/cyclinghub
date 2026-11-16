from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.riders import parse_riders_csv, cached_csv, store_csv
from core.powermodel import fit_cp_model, rho_air, wprime_balance
from core.course import parse_course, build_segments, fetch_weather, simulate
from core.fitexport import workout_from_parts

st.set_page_config(page_title="Prévision de perf — WT Performance Hub", page_icon="🎯", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks", "Manuel / CSV"],
                       key="prev_src")
athlete, pp, fixed_model = None, None, None
if src == "Manuel / CSV":
    mode_mc = st.sidebar.radio("Mode", ["Saisie manuelle", "Fichier CSV"], key="prev_mc_mode")
    if mode_mc == "Saisie manuelle":
        st.sidebar.caption("Coureur sans API : tu tapes ses valeurs à la main.")
        _name = st.sidebar.text_input("Nom", "Coureur manuel", key="prev_man_name")
        _cp = st.sidebar.number_input("CP (W)", 150, 700, 300, key="prev_man_cp")
        _wp = st.sidebar.number_input("W′ (kJ)", 2.0, 40.0, 20.0, 0.5, key="prev_man_wp")
        _kg = st.sidebar.number_input("Poids (kg)", 40.0, 110.0, 70.0, 0.5, key="prev_man_kg")
        athlete = {"id": "manuel", "name": _name, "role": "Manuel", "weight": float(_kg)}
        pp = {"cp": int(_cp)}
        fixed_model = {"cp": float(_cp), "wprime": float(_wp) * 1000.0}
    else:
        up_csv = st.sidebar.file_uploader("database_CP.csv", type=["csv"], key="prev_csv")
        csv_riders, csv_note = [], ""
        if up_csv is not None:
            csv_riders, csv_note = parse_riders_csv(up_csv.getvalue())
            if csv_note and not csv_note.startswith("("):
                st.sidebar.error(csv_note)
                csv_riders = []
            elif csv_riders:
                store_csv(up_csv.name, csv_riders)
                st.sidebar.success(f"{len(csv_riders)} coureur(s) chargé(s){csv_note}")
        elif cached_csv():
            cache = cached_csv()
            csv_riders = cache["riders"]
            st.sidebar.info(f"CSV conservé : **{cache['name']}** ({len(csv_riders)} coureur(s)) "
                            "— le même que dans Échauffement.")
        if not csv_riders:
            st.sidebar.info("Glisse ton `database_CP.csv` (le même que dans Échauffement) "
                            "ou passe en Saisie manuelle.")
            st.stop()
        _names = [str(r.get("name")) for r in csv_riders]
        _shared = st.session_state.get("rider_id")
        _ri = next((i for i, nm in enumerate(_names) if nm == _shared), 0)
        _sel = st.sidebar.selectbox("Coureur (CSV)", _names, index=_ri, key="prev_csv_rider")
        st.session_state["rider_id"] = _sel
        _row = next(r for r in csv_riders if str(r.get("name")) == _sel)
        athlete = {"id": _sel, "name": _sel, "role": "CSV", "weight": _row.get("weight") or 70.0}
        pp = {k: int(_row[k]) for k in ("p10s", "p20s", "p30s", "p1m", "p3m", "p5m",
                                        "p10m", "p20m") if _row.get(k)}
        pp["cp"] = int(_row["cp"])
        if not any(k in pp for k in ("p1m", "p5m", "p10m", "p20m")):
            fixed_model = {"cp": float(_row["cp"]),
                           "wprime": float(_row["wprime"]) * 1000.0 if _row.get("wprime")
                           else 20000.0}
else:
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
                                    key="prev_rider")
    st.session_state["rider_id"] = rider_id
    athlete = client.get_athlete(rider_id)
    pp = client.get_power_profile(rider_id)

st.title("🎯 Prévision de performance — pacing & watts cibles")
st.caption("Glissez un fichier .gpx ou .fit du parcours : modèle aéro/gravité/roulement calibré "
           "sur les données du coureur, météo gratuite Open-Meteo, stratégie de pacing type BestBikeSplit.")

up = st.file_uploader("Parcours (.gpx / .fit)", type=["gpx", "fit"])
if up is not None:
    try:
        points = parse_course(up.getvalue(), up.name)
        segs = build_segments(points)
        st.session_state["prev_course_cache"] = {"name": up.name, "points": points, "segs": segs}
    except Exception as e:
        st.error(f"Fichier illisible : {e}")
        st.stop()
else:
    cache = st.session_state.get("prev_course_cache")
    if cache:
        st.info(f"Fichier conservé de ta dernière visite : **{cache['name']}** — "
                "glisse un autre fichier pour le remplacer.")
        points, segs = cache["points"], cache["segs"]
    else:
        st.info("En attente d'un fichier de parcours.")
        st.stop()

total_km = segs["dist_m"].sum() / 1000.0
st.caption(f"🔍 Lecture du fichier : **{len(points)} points** · **{len(segs)} segments** · **{total_km:.1f} km**")
lats = [p["lat"] for p in points if p["lat"] is not None]
lons = [p["lon"] for p in points if p["lon"] is not None]
center = (float(np.mean(lats)), float(np.mean(lons))) if lats else None
dplus = float(np.clip(segs["gradient_pct"], 0, None).mul(segs["dist_m"], axis=0).sum() / 1000.0)

c1, c2, c3 = st.columns(3)
c1.metric("Distance", f"{total_km:.1f} km")
c2.metric("D+", f"{dplus:.0f} m")
c3.metric("Segments", len(segs))

# ── Paramètres coureur ───────────────────────────────────────────────────
if fixed_model is not None:
    model = dict(fixed_model)
else:
    pts = [(s, pp[k]) for k, s in [("p1m", 60), ("p3m", 180), ("p5m", 300),
                                   ("p10m", 600), ("p20m", 1200)] if pp.get(k)]
    model = fit_cp_model(pts) or {"cp": float(pp.get("cp") or 380), "wprime": 20000.0}

with st.expander("⚙️ Paramètres de simulation", expanded=True):
    p1, p2, p3, p4 = st.columns(4)
    mass = p1.number_input("Masse totale (kg)", 40.0, 120.0, float(athlete.get("weight") or 70.0) + 8.0, 0.5)
    scx = p2.number_input("SCx (m²)", 0.15, 0.50, float(st.session_state.get(f"scx_{athlete['id']}", 0.22)), 0.005)
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
wet_road = False
if use_weather and center:
    weather = fetch_weather(center[0], center[1], race_date.strftime("%Y-%m-%d"), race_hour)
    if "error" in weather:
        st.warning(weather["error"])
    else:
        rho = rho_air(weather["temp_c"], weather["pressure_hpa"])
        gusts = weather.get("gusts_ms", 0.0)
        wind_eff = max(weather["wind_ms"], 0.6 * gusts)
        wind_ms, wind_dir = wind_eff, weather["wind_dir"]
        wet_road = weather.get("precip_mm", 0.0) > 0.2
        gtxt = f" · rafales {gusts:.0f} m/s" if gusts > weather["wind_ms"] + 1 else ""
        ptxt = f" · pluie {weather['precip_mm']:.1f} mm" if wet_road else ""
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
    if wet_road:
        st.info("🌧️ Piste mouillée : Crr majoré de +0.0008 dans la simulation.")
    result = simulate(segs, mass, scx, rho, cp, wprime * 1000.0,
                      p_target=p_target, goal_time_s=goal_time_s,
                      crr=crr + (0.0008 if wet_road else 0.0),
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
    course_name = (up.name if up is not None
                   else st.session_state.get("prev_course_cache", {}).get("name", "course"))
    st.download_button("⬇️ Exporter le plan (CSV pour l'unité de compteur)", csv,
                       f"pacing_{athlete['id']}_{course_name}.csv", "text/csv")

# ── Plan simple pour le coureur (schéma + document imprimable) ──────────
def _img_uri(up):
    if up is None:
        return ""
    mime = "image/png" if (up.type or "").endswith("png") else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(up.getvalue()).decode()


def _grad_color(g):
    if g > 4:
        return "#dc2626"
    if g > 1.5:
        return "#f59e0b"
    if g < -1.5:
        return "#3b82f6"
    return "#22c55e"


def _group_parts(out, n, wind_ms, wind_dir):
    total = float(out["dist_m"].sum())
    edge = max(total / n, 1.0)
    cum, pids = 0.0, []
    for d in out["dist_m"].values:
        pids.append(min(n - 1, int(cum / edge)))
        cum += d
    parts = []
    for pid, sub in out.assign(part=pids).groupby("part"):
        hws = wind_ms * np.cos(np.radians(wind_dir - sub["bearing_deg"].values))
        parts.append({
            "n": pid + 1,
            "km_start": float(sub["start_km"].iloc[0]),
            "km_end": float(sub["start_km"].iloc[-1]) + float(sub["dist_m"].iloc[-1]) / 1000.0,
            "dist_km": float(sub["dist_m"].sum()) / 1000.0,
            "time_s": float(sub["time_s"].sum()),
            "watts": int(round(float(np.average(sub["power_w"], weights=sub["time_s"])))),
            "grad": float(np.average(sub["gradient_pct"], weights=sub["dist_m"])),
            "wind_kmh": float(np.average(hws, weights=sub["dist_m"])) * 3.6,
        })
    return parts


def _pacing_svg(parts, segs, total_km, power_curve=None):
    W, H = 900, 452
    PL, PR = 16, 16
    cw = W - PL - PR
    wind_y = 26
    box_top, bh, bw = 46, 64, 124
    py, ph = box_top + 2 * (bh + 10) + 6, 200
    def tx(km):
        return PL + cw * (km / max(total_km, 0.1))
    svg = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']
    svg.append(f'<defs><marker id="ahr" markerWidth="6" markerHeight="6" refX="4.4" refY="3" '
               f'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="#dc2626"/></marker>'
               f'<marker id="ahg" markerWidth="6" markerHeight="6" refX="4.4" refY="3" '
               f'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="#16a34a"/></marker></defs>')

    d_km = (segs["start_km"] + segs["dist_m"] / 1000.0).values
    if "ele" in segs.columns:
        ele = segs["ele"].values.astype(float)
    else:
        ele = (segs["gradient_pct"] / 100.0 * segs["dist_m"]).cumsum().values
    e_lo, e_hi = float(np.nanmin(ele)), float(np.nanmax(ele))
    span = max(e_hi - e_lo, 20.0)
    def ty(e):
        return py + ph - 8 - (ph - 16) * ((e - e_lo) / span)
    poly = " ".join(f"{tx(k):.1f},{ty(e):.1f}" for k, e in zip(d_km, ele))
    svg.append(f'<rect x="{PL}" y="{py}" width="{cw}" height="{ph}" fill="#f8fafc"/>')
    svg.append(f'<polygon points="{tx(0):.1f},{py + ph} {poly} {tx(total_km):.1f},{py + ph}" '
               f'fill="#dbeafe" stroke="#3b82f6" stroke-width="1.6"/>')

    for i, p in enumerate(parts):
        x1, x2 = tx(p["km_start"]), tx(p["km_end"])
        col = _grad_color(p["grad"])
        svg.append(f'<rect x="{x1:.1f}" y="{py + ph - 6}" width="{max(x2 - x1, 1):.1f}" height="6" '
                   f'fill="{col}"/>')
        if p["n"] > 1:
            svg.append(f'<line x1="{x1:.1f}" y1="{py}" x2="{x1:.1f}" y2="{py + ph}" '
                       f'stroke="#94a3b8" stroke-width="1" stroke-dasharray="4,3"/>')
        cx = (x1 + x2) / 2
        bx = min(max(cx - bw / 2, PL), PL + cw - bw)
        by = box_top if i % 2 == 0 else box_top + bh + 10
        svg.append(f'<rect x="{bx:.1f}" y="{by}" width="{bw}" height="{bh}" rx="6" '
                   f'fill="#ffffff" fill-opacity="0.92" stroke="{col}" stroke-width="2"/>')
        mm, ss = divmod(int(round(p["time_s"])), 60)
        bcx = bx + bw / 2
        svg.append(f'<text x="{bcx:.1f}" y="{by + 17}" text-anchor="middle" font-size="12" '
                   f'font-family="Arial" font-weight="bold" fill="#64748b">'
                   f'P{p["n"]} \u00b7 {p["km_start"]:.0f}\u2013{p["km_end"]:.0f} km</text>')
        svg.append(f'<text x="{bcx:.1f}" y="{by + 39}" text-anchor="middle" font-size="19" '
                   f'font-family="Arial" font-weight="900" fill="{col}">{p["watts"]} W</text>')
        svg.append(f'<text x="{bcx:.1f}" y="{by + 56}" text-anchor="middle" font-size="12" '
                   f'font-family="Arial" fill="#334155">{mm}:{ss:02d}</text>')

        wk = p["wind_kmh"]
        if abs(wk) >= 1.5:
            alen = min(56.0, 14.0 + abs(wk) * 1.6)
            headwind = wk > 0
            x_a = cx + (alen / 2 if headwind else -alen / 2)
            x_b = cx - (alen / 2 if headwind else -alen / 2)
            wcol = "#dc2626" if headwind else "#16a34a"
            mk = "ahr" if headwind else "ahg"
            svg.append(f'<line x1="{x_a:.1f}" y1="{wind_y}" x2="{x_b:.1f}" y2="{wind_y}" '
                       f'stroke="{wcol}" stroke-width="1.8" marker-end="url(#{mk})"/>')
            lbl = "head" if headwind else "tail"
            svg.append(f'<text x="{cx:.1f}" y="{wind_y - 9}" text-anchor="middle" font-size="10" '
                       f'font-family="Arial" font-weight="bold" fill="{wcol}">{abs(wk):.0f} {lbl}</text>')

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        km = total_km * frac
        svg.append(f'<line x1="{tx(km):.1f}" y1="{py + ph}" x2="{tx(km):.1f}" y2="{py + ph + 4}" '
                   f'stroke="#94a3b8" stroke-width="1"/>')
        anchor = "start" if frac == 0.0 else ("end" if frac == 1.0 else "middle")
        svg.append(f'<text x="{tx(km):.1f}" y="{py + ph + 15}" text-anchor="{anchor}" font-size="9" '
                   f'font-family="Arial" fill="#94a3b8">{km:.0f} km</text>')
    svg.append(f'<text x="{PL}" y="{py + 10}" font-size="9" font-family="Arial" fill="#94a3b8">'
               f'{e_hi:.0f} m</text>')
    svg.append(f'<text x="{PL}" y="{py + ph - 10}" font-size="9" font-family="Arial" '
               f'fill="#94a3b8">{e_lo:.0f} m</text>')

    if power_curve:
        pk = np.array([k for k, _ in power_curve])
        pw = np.array([w for _, w in power_curve], dtype=float)
        w_lo, w_hi = float(pw.min()), float(pw.max())
        w_span = max(w_hi - w_lo, 20.0)
        def wy(w):
            return py + ph - 8 - (ph - 30) * ((w - w_lo) / w_span)
        pts = " ".join(f"{tx(k):.1f},{wy(w):.1f}" for k, w in zip(pk, pw))
        svg.append(f'<polyline points="{pts}" fill="none" stroke="#dc2626" '
                   f'stroke-width="2" opacity="0.85"/>')
        svg.append(f'<text x="{PL + cw}" y="{py + 10}" text-anchor="end" font-size="9" '
                   f'font-family="Arial" font-weight="bold" fill="#dc2626">power (W)</text>')
    svg.append("</svg>")
    return "".join(svg)


def _print_doc(parts, segs, total_km, dplus, race, stage, date_str, logo_uri,
               total_time_str, avg_w, wind_lbl, power_curve=None, rider_name=""):
    rows = "".join(
        f"<tr><td><b>P{p['n']}</b></td><td>{p['km_start']:.0f}–{p['km_end']:.0f} km</td>"
        f"<td>{p['dist_km']:.1f} km</td><td><b>{p['watts']} W</b></td>"
        f"<td>{divmod(int(round(p['time_s'])), 60)[0]}:{divmod(int(round(p['time_s'])), 60)[1]:02d}</td>"
        f"<td>{p['grad']:+.1f} %</td><td>{abs(p['wind_kmh']):.0f} km/h "
        f"{'head' if p['wind_kmh'] > 0 else ('tail' if p['wind_kmh'] < 0 else '—')}</td></tr>"
        for p in parts)
    logo = f'<img src="{logo_uri}" style="height:46px;max-width:180px;object-fit:contain">' if logo_uri else \
           '<div style="font-weight:900;letter-spacing:3px;color:rgba(255,255,255,.5)">LOGO</div>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4 portrait; margin: 10mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #111318; background: #ffffff; }}
.hero {{ background: #111318; color: #fff; padding: 16px 20px; display: flex;
        align-items: center; gap: 16px; border-radius: 8px 8px 0 0; }}
.hero h1 {{ font-size: 22px; letter-spacing: 2px; text-transform: uppercase; }}
.hero .sub {{ font-size: 11px; color: rgba(255,255,255,.6); letter-spacing: 1px;
             text-transform: uppercase; }}
.durbox {{ margin-left: auto; text-align: center; background: rgba(255,255,255,.1);
          padding: 8px 16px; border-radius: 6px; }}
.durbox b {{ font-size: 20px; }} .durbox span {{ font-size: 10px; color: #f7e982; }}
.racebar {{ display: flex; gap: 0; border: 1px solid #dde0e8; border-top: none; }}
.racebar div {{ flex: 1; padding: 7px 10px; border-right: 1px solid #dde0e8; }}
.racebar div:last-child {{ border-right: none; }}
.racebar .lbl {{ font-size: 9px; letter-spacing: 1px; text-transform: uppercase;
                color: #7a8194; }}
.racebar .val {{ font-size: 13px; font-weight: 700; }}
.chart {{ padding: 18px 6px 6px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 12px; }}
th {{ background: #f4f5f7; text-transform: uppercase; font-size: 9px; letter-spacing: 1px;
     color: #7a8194; padding: 6px 8px; text-align: left; }}
td {{ padding: 6px 8px; border-bottom: 1px solid #dde0e8; }}
.note {{ margin-top: 12px; font-size: 10px; color: #7a8194; }}
.printbtn {{ position: fixed; top: 12px; right: 12px; background: #111318; color: #fff;
            border: none; padding: 10px 18px; border-radius: 6px; font-weight: bold;
            cursor: pointer; }}
@media print {{ .printbtn {{ display: none; }} }}
</style></head><body>
<button class="printbtn" onclick="window.print()">🖨️ Print / PDF</button>
<div class="hero">{logo}<div><div class="sub">Race plan — pacing · {rider_name}</div>
<h1>Race strategy — {race or '—'}</h1></div>
<div class="durbox"><span>Estimated time</span><br><b>{total_time_str}</b><br>
<span>{avg_w:.0f} W avg</span></div></div>
<div class="racebar">
<div><div class="lbl">Race</div><div class="val">{race or '—'}</div></div>
<div><div class="lbl">Stage</div><div class="val">{stage or '—'}</div></div>
<div><div class="lbl">Date</div><div class="val">{date_str or '—'}</div></div>
<div><div class="lbl">Distance</div><div class="val">{total_km:.1f} km · {dplus:.0f} m D+</div></div>
<div><div class="lbl">Wind</div><div class="val">{wind_lbl}</div></div>
</div>
<div class="chart">{_pacing_svg(parts, segs, total_km, power_curve)}</div>
<table><tr><th>Part</th><th>Km</th><th>Length</th><th>Power</th><th>Time</th>
<th>Gradient</th><th>Wind</th></tr>{rows}</table>
<div class="note">Headwind = red arrow pointing left. Tailwind = green arrow pointing right.
Colors: green flat · orange climb · red steep · blue descent. Red curve = target power (W).</div>
</body></html>"""


st.subheader("🚴 Plan simple pour le coureur")
st.caption("Synthèse lisible : grandes parties, watts, durées et vent. "
           "Le curseur choisit le nombre de parties. Imprime en PDF et envoie-le à ton coureur.")
pa, pb, pc = st.columns(3)
n_parts = pa.slider("Nombre de parties", 2, 15, 6)
race_name = pb.text_input("Course", key="prev_race")
stage_name = pc.text_input("Étape / parcours", key="prev_stage")
pe, pf = st.columns(2)
default_day = race_date if (use_weather and race_date) else date.today()
race_day = pe.date_input("Date", value=default_day, key="prev_day")
logo_up = pf.file_uploader("Logo (png/jpg)", type=["png", "jpg", "jpeg"], key="prev_logo")

parts = _group_parts(out, n_parts, wind_ms, wind_dir)
total_time_str = f"{int(h):02d}:{int(mn):02d}:{int(s):02d}"
wind_lbl = (f"{abs(wind_ms) * 3.6:.0f} km/h" if wind_ms else "non pris en compte")
power_curve = list(zip((out["start_km"] + out["dist_m"] / 1000.0).values,
                       out["power_w"].values))
fit_bytes = workout_from_parts(parts, f"Race plan {athlete['name']}")
st.download_button("⬇️ Exporter le plan pour le compteur (.fit)", fit_bytes,
                   f"race_plan_{athlete['name'].replace(' ', '_')}.fit",
                   "application/octet-stream")
doc = _print_doc(parts, segs, total_km, dplus, race_name, stage_name,
                 race_day.strftime("%d/%m/%Y"), _img_uri(logo_up),
                 total_time_str, result["avg_w"], wind_lbl, power_curve,
                 rider_name=athlete["name"])
components.html(doc, height=1050, scrolling=True)

st.divider()
with st.expander("📊 Prévision vs réel (calibrage du modèle)"):
    hist = st.session_state.setdefault("prev_history", [])
    if st.button("💾 Enregistrer cette prévision"):
        hist.append({"course": race_name or "—", "date": race_day.strftime("%d/%m/%Y"),
                     "km": round(total_km, 1), "temps_estime": total_time_str,
                     "est_s": int(h * 3600 + mn * 60 + s), "w_moyen": int(result["avg_w"])})
        st.rerun()
    if hist:
        dfh = pd.DataFrame(hist)
        st.dataframe(dfh, hide_index=True, use_container_width=True)
        i = st.selectbox("Course à comparer", range(len(hist)),
                         format_func=lambda k: f"{hist[k]['course']} — {hist[k]['date']}")
        rc1, rc2, rc3 = st.columns(3)
        rh = rc1.number_input("h", 0, 8, 0, key="real_h")
        rmn = rc2.number_input("min", 0, 59, 0, key="real_m")
        rs = rc3.number_input("s", 0, 59, 0, key="real_s")
        real_s = rh * 3600 + rmn * 60 + rs
        if real_s > 0:
            est = hist[i]["est_s"]
            d = (real_s - est) / est * 100
            st.metric("Écart prévision / réel", f"{d:+.1f} %",
                      "modèle optimiste" if d > 0 else "modèle pessimiste")
        st.download_button("⬇️ Historique (CSV)", dfh.to_csv(index=False).encode(),
                           "historique_previsions.csv", "text/csv")
        if st.button("🗑️ Vider l'historique"):
            st.session_state["prev_history"] = []
            st.rerun()
    else:
        st.caption("Enregistre une prévision avant la course, puis saisis le temps réel "
                   "après : l'écart te dit si le modèle est optimiste ou pessimiste "
                   "pour ce coureur.")

st.caption("Modèle : P = ½·ρ·SCx·v²·v + m·g·(pente + Crr)·v, rendement chaîne inclus. "
           "Le pacing surpondère les sections en montée (sensibilité de la vitesse à la puissance) "
           "et sous-alimente les descentes — logique BestBikeSplit.")
