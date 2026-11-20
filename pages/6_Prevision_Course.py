import html as _html
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.riders import parse_riders_csv, cached_csv, store_csv
from core.powermodel import fit_cp_model, rho_air
from core.course import parse_course, build_segments
from core.raceplan import ROLES, cho_grams_per_hour, detect_climbs, hydratation_l_per_h, power_for_time, race_plan

st.set_page_config(page_title="Prévision Course — WT Performance Hub",
                   page_icon="🏁", layout="wide")
gate()

BIKE_KG = 8.0


def _fmt(v, dec=0):
    v = None if v is None else float(v)
    if v is None or not np.isfinite(v):
        return "—"
    return f"{v:,.{dec}f}".replace(",", " ")


def _hms(s):
    if s is None or not np.isfinite(s):
        return "—"
    s = int(round(s))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _parse_rec(s):
    """« h:mm:ss » ou « mm:ss » ou minutes décimales → secondes."""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d+):\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?$", s)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return a * 3600 + b * 60 + (c if m.group(3) else 0)
    try:
        return float(s.replace(",", ".")) * 60.0
    except ValueError:
        return None


# ── Source des coureurs (même logique que Testing) ────────────────────────────
src = st.sidebar.radio("Source des données",
                       ["Vekta (API)", "TrainingPeaks", "Fichier CSV"],
                       key="race_src")
if st.sidebar.button("🔄 Recharger les données"):
    for k in ("race_riders", "race_sig", "race_note"):
        st.session_state.pop(k, None)
    st.rerun()

up = (st.sidebar.file_uploader("database_CP.csv", type=["csv"])
      if src == "Fichier CSV" else None)
sig = (src, up.name if up else "", len(up.getvalue()) if up else 0)

if st.session_state.get("race_sig") != sig:
    riders, notes = [], []
    if src == "TrainingPeaks":
        tp = session_client()
        if not tp.configured:
            notes.append(("warning", "TrainingPeaks : ajoute TP_USERNAME et TP_PASSWORD "
                                     "dans les Secrets Streamlit."))
        else:
            tp_riders = tp.get_athletes()
            if not tp_riders:
                notes.append(("warning", f"TrainingPeaks : {tp.last_error}"))
            else:
                for a in tp_riders:
                    cp = tp.get_cp_estimate(a["id"]).get("cp")
                    if not cp:
                        continue
                    riders.append({"name": a.get("name", a["id"]),
                                   "team": a.get("team", a.get("role", "Team")),
                                   "hrmax": a.get("hrmax", 185),
                                   "weight": a.get("weight", 70), "cp": cp})
    elif src == "Vekta (API)":
        vekta = VektaClient()
        athletes = vekta.get_athletes()
        if vekta.last_error:
            notes.append(("warning", f"Vekta : {vekta.last_error} — données de "
                                     "démonstration affichées."))
        elif vekta.notice:
            notes.append(("info", vekta.notice))
        for a in athletes:
            pp = vekta.get_power_profile(a["id"])
            riders.append({"name": a.get("name", a["id"]),
                           "team": a.get("team", a.get("role", "Team")),
                           "hrmax": a.get("hrmax", 185),
                           "weight": a.get("weight", 70), **pp})
    elif up:
        riders, note = parse_riders_csv(up.getvalue())
        if note and not note.startswith("("):
            notes.append(("error", note))
            riders = []
        elif riders:
            store_csv(up.name, riders)
            notes.append(("success", f"{len(riders)} coureur(s) chargé(s) depuis le "
                                     f"CSV{note}"))
    elif cached_csv():
        cache = cached_csv()
        riders = cache["riders"]
        notes.append(("info", f"CSV conservé : **{cache['name']}** "
                              f"({len(riders)} coureur(s))."))
    st.session_state["race_riders"] = riders
    st.session_state["race_sig"] = sig
    st.session_state["race_note"] = notes

riders = st.session_state.get("race_riders") or []
for level, msg in st.session_state.get("race_note", []):
    getattr(st.sidebar, level)(msg)

st.title("🏁 Prévision Course")
st.caption("Montées détectées du fichier (.gpx/.fit), records relevés (Strava / KOM, "
           "saisis à la main), temps par rôle : leaders à bloc, coéquipiers et "
           "sprinters gèrent pour rester dans les délais. Durée, délai (fenêtre de "
           "contrôle), kJ et glucides g/h. Fiche par coureur + document global.")

with st.expander("➕ Ajouter des coureurs manuellement (hors plateforme)"):
    new_names = st.text_area("Un nom par ligne", key="race_manual_input", height=70,
                             placeholder="Nom Prénom")
    m1, m2 = st.columns(2)
    man_w = m1.number_input("Poids (kg)", 30.0, 120.0, 70.0, 0.5, key="race_man_w")
    man_cp = m2.number_input("CP (W)", 150, 700, 300, key="race_man_cp")
    if st.button("Ajouter"):
        known = {str(r.get("name")) for r in riders}
        added = 0
        for line in (new_names or "").splitlines():
            nm = line.strip()
            if nm and nm not in known:
                st.session_state.setdefault("race_manual", []).append(
                    {"name": nm, "team": "Manuel", "hrmax": 185,
                     "weight": man_w, "cp": man_cp})
                known.add(nm)
                added += 1
        if added:
            st.rerun()

manual = st.session_state.get("race_manual", [])
known = {str(r.get("name")) for r in riders}
riders = riders + [m for m in manual if str(m.get("name")) not in known]
if not riders:
    st.info("Aucun coureur — choisis une source dans la barre latérale ou ajoute-les "
            "manuellement ci-dessus.")
    st.stop()

# ── 1 · Fichier de course ──────────────────────────────────────────────────────
st.subheader("1 · Parcours (.gpx / .fit)")
up_race = st.file_uploader("Fichier de la course", type=["gpx", "fit"], key="race_file")
if st.session_state.get("race_cache") and (up_race is None):
    cache = st.session_state["race_cache"]
    st.caption(f"Fichier conservé : **{cache['name']}** — glisse un autre fichier "
               "pour le remplacer.")
    points, segs = cache["points"], cache["segs"]
elif up_race is not None:
    try:
        points = parse_course(up_race.getvalue(), up_race.name)
        segs = build_segments(points)
        st.session_state["race_cache"] = {"name": up_race.name, "points": points,
                                          "segs": segs, "file": up_race.name}
        points, segs = points, segs
    except Exception as e:
        st.error(f"Fichier illisible : {type(e).__name__} — {e}")
        st.stop()
else:
    st.info("Glisse le fichier .gpx ou .fit de la course (Pro Cycling Stats, "
            "Strava, organiseur).")
    st.stop()

race_name = st.session_state["race_cache"]["name"].rsplit(".", 1)[0]
if "race_climb_rows" not in st.session_state or \
        st.session_state.get("race_climb_src") != race_name:
    climbs0 = detect_climbs(segs)
    st.session_state["race_climb_rows"] = pd.DataFrame(
        [{"km": c["start_km"], "Longueur (km)": c["len_km"],
          "Pente (%)": c["grad_pct"], "D+ (m)": c["dplus_m"],
          "Nom / Strava": "", "Record": ""} for c in climbs0])
    st.session_state["race_climb_src"] = race_name

total_km = segs["dist_m"].sum() / 1000.0
dplus = float(np.clip(segs["gradient_pct"], 0, None).mul(segs["dist_m"], axis=0)
              .sum() / 1000.0)
c1, c2, c3 = st.columns(3)
c1.metric("Distance", f"{total_km:.1f} km")
c2.metric("D+", f"{dplus:.0f} m")
c3.metric("Montées détectées", len(st.session_state["race_climb_rows"]))

fig = go.Figure()
fig.add_trace(go.Scatter(x=segs["start_km"], y=segs["ele"], mode="lines",
                         fill="tozeroy", line=dict(color="#2563eb", width=1),
                         name="Élévation"))
for _, c in st.session_state["race_climb_rows"].iterrows():
    fig.add_vrect(x0=float(c["km"]), x1=float(c["km"]) + float(c["Longueur (km)"] or 0),
                  fillcolor="rgba(220,38,38,0.12)", line_width=0)
fig.update_layout(template="plotly_white", height=260,
                  xaxis_title="km", yaxis_title="Élévation (m)",
                  margin=dict(l=0, r=0, t=10, b=0))
st.plotly_chart(fig, use_container_width=True)

# ── 2 · Montées & records ──────────────────────────────────────────────────────
st.subheader("2 · Montées principales & records")
st.caption("Les montées sont détectées automatiquement ; complète le nom (Strava) et "
           "le record à battre (« mm:ss » ou « h:mm:ss », ou minutes décimales). "
           "Tu peux aussi ajouter une montée oubliée (dynamic).")
try:
    with st.form("climb_form", clear_on_submit=False):
        cdf = st.data_editor(
            st.session_state["race_climb_rows"], key="race_climbs_ed",
            hide_index=True, use_container_width=True, num_rows="dynamic",
            column_config={
                "km": st.column_config.NumberColumn("km", disabled=True),
                "Longueur (km)": st.column_config.NumberColumn("Longueur (km)",
                                                               disabled=True),
                "Pente (%)": st.column_config.NumberColumn("Pente (%)", disabled=True),
                "D+ (m)": st.column_config.NumberColumn("D+ (m)", disabled=True),
                "Nom / Strava": st.column_config.TextColumn("Nom / Strava"),
                "Record": st.column_config.TextColumn("Record (mm:ss)",
                                                      help="« 24:35 », « 1:02:40 » "
                                                           "ou « 24,6 » (minutes)"),
            })
        cdf = st.form_submit_button("💾 Enregistrer montées & records")
except Exception:
    st.session_state["race_climb_rows"] = pd.DataFrame(
        [{"km": None, "Longueur (km)": None, "Pente (%)": None, "D+ (m)": None,
          "Nom / Strava": "", "Record": ""}])
    st.warning("Tableau réinitialisé (état incompatible détecté).")
    st.rerun()
if cdf:
    st.session_state["race_climb_rows"] = cdf

climb_rows = st.session_state["race_climb_rows"].dropna(
    subset=["km", "Longueur (km)", "Pente (%)"])
climb_rows = climb_rows[climb_rows["Pente (%)"] > 0].sort_values("km")
climb_list = [(float(r["km"]), float(r["Longueur (km)"]), float(r["Pente (%)"]))
              for _, r in climb_rows.iterrows()]
records = [_parse_rec(r["Record"]) for _, r in climb_rows.iterrows()]
rest_km = max(total_km - sum(l for _, l, _ in climb_list), 0.0)
grad_rest = float(
    np.average(np.clip(segs["gradient_pct"], -1.5, 2.0),
               weights=segs["dist_m"])) if len(segs) else 0.0
climb_km = sum(l for _, l, _ in climb_list)

# ── 3 · Équipe & rôles ─────────────────────────────────────────────────────────
st.subheader("3 · Équipe engagée & rôles")
names = [str(r.get("name")) for r in riders]
prev_sel = [n for n in st.session_state.get("race_sel", []) if n in names]
team_sel = st.multiselect("Coureurs engagés", names,
                          default=prev_sel or names[: min(15, len(names))],
                          max_selections=40)
st.session_state["race_sel"] = team_sel
if not team_sel:
    st.info("Sélectionne au moins un coureur.")
    st.stop()

rider_by = {str(r.get("name")): r for r in riders}
roles_saved = st.session_state.get("race_roles", {})


def _model_of(n):
    r = rider_by.get(n, {})
    pts = [(s, r[k]) for k, s in [("p1m", 60), ("p3m", 180), ("p5m", 300),
                                  ("p10m", 600), ("p20m", 1200)] if r.get(k)]
    m = fit_cp_model(pts) if len(pts) >= 2 else None
    cp = m["cp"] if m else (float(r.get("cp") or 320))
    return cp, (m["wprime"] / 1000.0 if m else 21.0)


role_rows = []
for n in team_sel:
    r = rider_by.get(n, {})
    cp, wp = _model_of(n)
    saved_r = roles_saved.get(n, {})
    role_rows.append({"Coureur": n,
                      "Rôle": saved_r.get("role", "Coéquipier"),
                      "Poids (kg)": float(saved_r.get("kg") or r.get("weight") or 70.0),
                      "CP (W)": float(saved_r.get("cp") or cp),
                      "W′ (kJ)": float(saved_r.get("wp") or round(wp))})
try:
    with st.form("role_form", clear_on_submit=False):
        rodf = st.data_editor(
            pd.DataFrame(role_rows), key="race_roles_ed",
            hide_index=True, use_container_width=True, num_rows="dynamic",
            column_config={
                "Coureur": st.column_config.TextColumn("Coureur", disabled=True),
                "Rôle": st.column_config.SelectboxColumn("Rôle", options=ROLES,
                                                         required=True),
                "Poids (kg)": st.column_config.NumberColumn("Poids (kg)",
                                                            min_value=40.0,
                                                            max_value=110.0, step=0.5),
                "CP (W)": st.column_config.NumberColumn("CP (W)", min_value=150,
                                                        max_value=700, step=5),
                "W′ (kJ)": st.column_config.NumberColumn("W′ (kJ)", min_value=4.0,
                                                         max_value=40.0, step=0.5),
            })
        save_roles = st.form_submit_button("💾 Enregistrer l'équipe")
except Exception:
    rodf = pd.DataFrame(role_rows)
    st.warning("Tableau rôles réinitialisé.")
if save_roles:
    st.session_state["race_roles"] = {
        str(r["Coureur"]): {"role": r["Rôle"], "kg": r["Poids (kg)"],
                            "cp": r["CP (W)"], "wp": r["W′ (kJ)"]}
        for _, r in rodf.iterrows() if str(r["Coureur"]).strip()}

# ── 4 · Paramètres de course ──────────────────────────────────────────────────
st.subheader("4 · Paramètres de course")
p1, p2, p3, p4 = st.columns(4)
scenario = p1.radio("Scénario", ["Rapide", "Normal", "Lent"], horizontal=True,
                    key="race_scen",
                    help="Vitesse du peloton sur le roulant : change les temps, "
                         "l'énergie et les glucides.")
delai = p2.number_input("Délai / fenêtre (%, du temps gagnant)", 5, 40, 20, 1,
                        key="race_delai",
                        help="Le % que tu remplis selon la course (règlement / "
                             "expérience). Fenêtre = temps gagnant × (1 + %).")
winner_txt = p3.text_input("Temps gagnant à toi de fixer (h:mm:ss) — optionnel",
                           key="race_winner", placeholder="ex : 4:45:00",
                           help="Si tu remplis, il remplace le temps gagnant estimé.")
scx = p4.number_input("SCx moyen (m²)", 0.15, 0.40, 0.22, 0.005, key="race_scx")
p5, p6, p7 = st.columns(3)
temp_c = p5.number_input("Température (°C)", -5, 40, 18, 1, key="race_temp")
leader_mode = p6.radio("Objectif leader", ["Max (modèle CP)", "Records saisis"],
                       horizontal=True, key="race_lmode",
                       help="« Records saisis » : les leaders visent les temps "
                            "relevés (Strava/KOM) ci-dessus.")
fatigue = p7.number_input("Fatigue leader (% de perf en moins)", 0, 25, 0, 1,
                          key="race_fat",
                          help="Perf sur record avec de la km dans les jambes : "
                               "5 = 5 % de CP/W′ en moins sur les montées.")
rho = rho_air(temp_c, 1013.0)

# ── 5 · Calculs ────────────────────────────────────────────────────────────────
if not climb_list:
    st.warning("Aucune montée retenue — vérifie le fichier ou ajoute-les à la main "
               "dans le tableau.")
    st.stop()

plans = {}
for _, row in rodf.iterrows():
    n = str(row["Coureur"]).strip()
    if not n:
        continue
    rider = {"cp": float(row["CP (W)"]), "wprime": float(row["W′ (kJ)"]) * 1000.0,
             "mass_total": float(row["Poids (kg)"]) + BIKE_KG}
    plans[n] = {"role": str(row["Rôle"]), "kg": float(row["Poids (kg)"]),
                **race_plan(climb_list, rest_km, grad_rest, rider, str(row["Rôle"]),
                            scenario, rho, wind_ms=0.0, fatigue_pct=fatigue,
                            records=records,
                            leader_record=(leader_mode == "Records saisis"))}

winner_s = _parse_rec(winner_txt) or min(p["total_s"] for p in plans.values())
cut_s = winner_s * (1.0 + delai / 100.0)

# ── 6 · Résultats ──────────────────────────────────────────────────────────────
st.subheader("5 · Temps, délais, kJ, alimentation")
sum_rows = []
for n, pl in plans.items():
    lin_h = pl["total_s"] / 3600.0
    cho_h = cho_grams_per_hour(lin_h, pl["avg_wkg"], scenario)
    hyd = hydratation_l_per_h(lin_h, pl["avg_wkg"])
    margin = cut_s - pl["total_s"]
    sum_rows.append({
        "Coureur": n, "Rôle": pl["role"],
        "Temps": _hms(pl["total_s"]),
        "Écart gagnant": _hms(pl["total_s"] - winner_s),
        "Marge délai": _hms(margin),
        "W roulant": round(pl["rest_w"]),
        "W moyen": round(pl["avg_w"]),
        "kJ": round(pl["kj"]),
        "g/h": int(cho_h), "Glucides (g)": int(cho_h * lin_h),
        "Boisson (L/h)": round(hyd, 1),
        "_niv": "rouge" if margin < 0 else ("orange" if margin < 300 else "")})
sum_df = pd.DataFrame(sum_rows)
if not sum_df.empty and plans:
    order = {"Leader": 0, "Coéquipier": 1, "Sprinter": 2}
    sum_df = sum_df.sort_values(["Rôle", "Temps"],
                                key=lambda c: c.map(order) if c.name == "Rôle"
                                else c)

    def _paint(row):
        color = ("#fee2e2" if row["_niv"] == "rouge"
                 else "#fef3c7" if row["_niv"] else "")
        return [f"background-color:{color}"] * len(row) if color else [""] * len(row)

    st.dataframe(sum_df.style.apply(_paint, axis=1).hide(axis="index"),
                 use_container_width=True)
    st.caption(f"Temps gagnant estimé : **{_hms(winner_s)}** · fenêtre de contrôle : "
               f"**{_hms(cut_s)}** (délai {delai:.0f} %). 🟠 marge < 5 min · "
               "🔴 hors délais.")

# Montées : temps & watts par coureur
climb_tab_rows = []
for i, (km0, ln, gr) in enumerate(climb_list):
    row = {"km": km0, "Longueur (km)": ln, "Pente (%)": gr,
           "Record": _hms(records[i]) if records[i] else "—"}
    for n, pl in plans.items():
        if i < len(pl["climbs"]):
            cd = pl["climbs"][i]
            row[n] = f"{_hms(cd['time_s'])} · {cd['power_w']:.0f} W"
    climb_tab_rows.append(row)
st.markdown("**Montées — temps · puissance par coureur**")
st.dataframe(pd.DataFrame(climb_tab_rows), hide_index=True, use_container_width=True)
st.caption(f"Roulant : {rest_km:.1f} km (pente moy. {grad_rest:+.1f} %) · "
           f"montées : {climb_km:.1f} km.")

# Profile : pubs
st.download_button("⬇️ Résumé course (CSV)",
                   sum_df.drop(columns=["_niv"]).to_csv(
                       index=False, sep=";", decimal=",").encode("utf-8-sig"),
                   "prevision_course.csv", "text/csv")

# ── 7 · Documents ──────────────────────────────────────────────────────────────
st.subheader("6 · Documents imprimables")
doc_kind = st.radio("Document", ["Fiche coureur", "Document global"],
                    horizontal=True, key="race_doc")
if doc_kind == "Fiche coureur":
    fiche_n = st.selectbox("Coureur", list(plans.keys()), key="race_doc_rider")
else:
    fiche_n = None


def _esc(x):
    return _html.escape(str(x))


def _fiche_html(n):
    pl = plans[n]
    margin = cut_s - pl["total_s"]
    lin_h = pl["total_s"] / 3600.0
    cho_h = cho_grams_per_hour(lin_h, pl["avg_wkg"], scenario)
    hyd = hydratation_l_per_h(lin_h, pl["avg_wkg"])
    rows = ""
    for cd in pl["climbs"]:
        pw = int(round(cd["power_w"]))
        rows += (f"<tr><td>{_esc(cd['start_km'])} km</td>"
                 f"<td>{_esc(cd['len_km'])} km</td>"
                 f"<td>{_esc(cd['grad_pct'])} %</td>"
                 f"<td>{_esc(_hms(cd['time_s']))}</td>"
                 f"<td>{_esc(pw)} W</td></tr>")
    margin_txt = ("HORS DÉLAIS" if margin < 0
                  else f"marge {_esc(_hms(margin))} avant la fermeture "
                       f"({_esc(_hms(cut_s))})")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111;background:#fff;margin:24px}}
    h1{{font-size:20px;margin:0 0 2px}} h2{{font-size:14px;margin:14px 0 4px;color:#333}}
    table{{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}}
    td,th{{border:1px solid #ddd;padding:4px 6px;text-align:left}}
    th{{background:#f1f5f9}}
    .kv{{font-size:12px;color:#444;margin:2px 0}}
    .ok{{color:#15803d;font-size:12px}}
    @media print{{body{{margin:10mm}}}}
    </style></head><body>
    <h1>🏁 {_esc(race_name)}</h1>
    <div class="kv">{_esc(n)} — {_esc(pl['role'])} · {_esc(_fmt(total_km, 1))} km ·
    D+ {_esc(_fmt(dplus))} m · {_esc(_hms(pl['total_s']))} · {_esc(_fmt(pl['avg_w']))} W moy · {_esc(_fmt(pl['kj']))} kJ</div>
    <div class="ok">{margin_txt}</div>
    <h2>Montées — objectif temps / watts</h2>
    <table><tr><th>km</th><th>Longueur</th><th>Pente</th><th>Temps</th><th>Watts</th></tr>{rows}</table>
    <h2>Roulant</h2>
    <div class="kv">{_esc(_fmt(rest_km, 1))} km à ~{_esc(_fmt(pl['rest_w']))} W (scénario {_esc(scenario)})</div>
    <h2>Alimentation ({_esc(scenario)})</h2>
    <div class="kv">{_esc(int(cho_h))} g/h de glucides → {_esc(int(cho_h * lin_h))} g au total ·
    boisson ~{_esc(f"{hyd:.1f}")} L/h · transport mixte conseillé au-delà de 2h30
    (glucose:fructose ~1:0,8)</div>
    <h2>Délai / fenêtre de contrôle</h2>
    <div class="kv">Temps gagnant {_esc(_hms(winner_s))} · délai {_esc(f"{delai:.0f} %")}
    → fenêtre ferme à {_esc(_hms(cut_s))}</div>
    </body></html>"""


def _global_html():
    trs = ""
    for _, r in sum_df.iterrows():
        trs += (f"<tr><td>{_esc(r['Coureur'])}</td><td>{_esc(r['Rôle'])}</td>"
                f"<td>{_esc(r['Temps'])}</td><td>{_esc(r['Écart gagnant'])}</td>"
                f"<td>{_esc(r['Marge délai'])}</td><td>{_esc(r['W roulant'])}</td>"
                f"<td>{_esc(r['W moyen'])}</td><td>{_esc(r['kJ'])}</td>"
                f"<td>{_esc(r['g/h'])}</td><td>{_esc(r['Glucides (g)'])}</td></tr>")
    crs = "".join(
        f"<tr><td>{_esc(f'{km0:.1f} km')}</td><td>{_esc(f'{ln:.2f} km')}</td>"
        f"<td>{_esc(f'{gr:.1f} %')}</td>"
        f"<td>{_esc(_hms(records[i]) if records[i] else '—')}</td></tr>"
        for i, (km0, ln, gr) in enumerate(climb_list))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111;background:#fff;margin:24px}}
    h1{{font-size:20px;margin:0 0 2px}} h2{{font-size:14px;margin:14px 0 4px;color:#333}}
    table{{border-collapse:collapse;width:100%;font-size:11px;margin-top:6px}}
    td,th{{border:1px solid #ddd;padding:3px 5px;text-align:left}}
    th{{background:#f1f5f9}}
    .kv{{font-size:12px;color:#444;margin:2px 0}}
    @media print{{body{{margin:10mm}}}}
    </style></head><body>
    <h1>🏁 {_esc(race_name)} — plan d'équipe</h1>
    <div class="kv">{_esc(_fmt(total_km, 1))} km · D+ {_esc(_fmt(dplus))} m · scénario {_esc(scenario)} · temps gagnant {_esc(_hms(winner_s))} · fenêtre {_esc(_hms(cut_s))} (délai {_esc(f'{delai:.0f} %')})</div>
    <h2>Équipe</h2>
    <table><tr><th>Coureur</th><th>Rôle</th><th>Temps</th><th>Écart</th><th>Marge délai</th><th>W roulant</th><th>W moy</th><th>kJ</th><th>g/h</th><th>Gluc.</th></tr>{trs}</table>
    <h2>Montées</h2>
    <table><tr><th>km</th><th>Longueur</th><th>Pente</th><th>Record</th></tr>{crs}</table>
    </body></html>"""


if doc_kind == "Fiche coureur" and fiche_n in plans:
    with st.expander(f"🖨️ Fiche {fiche_n} — imprimer / PDF (Ctrl+P)", expanded=True):
        components.html(_fiche_html(fiche_n), height=780, scrolling=True)
else:
    with st.expander("🖨️ Document global équipe — imprimer / PDF (Ctrl+P)",
                     expanded=True):
        components.html(_global_html(), height=800, scrolling=True)
