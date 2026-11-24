import io

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.riders import parse_riders_csv, cached_csv, store_csv
from core.lactate import (SV1_METHODS, baseline_lactate, clean_steps, sv1,
                          sv1_block_verdict, sv1_trend, sv2, zone_table,
                          zone_table_partial)
from core.powermodel import (CP_ZONE_PCTS, FC_ZONE_PCTS, cp_from_5_20,
                             cp_zone_table, fc_zone_table)

st.set_page_config(page_title="Testing — WT Performance Hub", page_icon="🧪", layout="wide")
gate()

GROUPS = ["WT Hommes", "WT Femmes", "Conti Hommes", "Conti Femmes"]

src = st.sidebar.radio("Source des données",
                       ["Vekta (API)", "TrainingPeaks", "Fichier CSV"], key="test_src")
if st.sidebar.button("🔄 Recharger les données"):
    for k in ("test_riders", "test_sig", "test_src_note"):
        st.session_state.pop(k, None)
    st.rerun()

up = (st.sidebar.file_uploader("database_CP.csv", type=["csv"])
      if src == "Fichier CSV" else None)
sig = (src, up.name if up else "", len(up.getvalue()) if up else 0)

if st.session_state.get("test_sig") != sig:
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
    st.session_state["test_riders"] = riders
    st.session_state["test_sig"] = sig
    st.session_state["test_src_note"] = notes

riders = st.session_state.get("test_riders") or []
for level, msg in st.session_state.get("test_src_note", []):
    getattr(st.sidebar, level)(msg)

st.title("🧪 Testing — lactate & SV1")
st.caption("Test lactate par paliers → seuils SV1/SV2, zones watts/FC et rapport. "
           "Test SV1 3×15' → zone de travail calculée en amont (plancher/plafond en watts et FC), lactates par bloc, analyse par coureur "
           "et par groupe. Les données restent dans ta session — exporte en CSV pour les garder.")

with st.expander("➕ Ajouter des coureurs manuellement (hors Vekta / TP)"):
    st.caption("Utile pour un test avec des coureurs hors plateforme — ils restent "
               "dans ta session.")
    new_names = st.text_area("Un nom par ligne", key="test_manual_input", height=80,
                             placeholder="Nom Prénom")
    mc1, mc2 = st.columns(2)
    manual_w = mc1.number_input("Poids par défaut (kg)", 30.0, 120.0, 70.0, 0.5,
                                key="test_manual_weight")
    if mc2.button("Ajouter ces coureurs"):
        existing = {str(r.get("name")) for r in riders}
        added = 0
        for line in (new_names or "").splitlines():
            nm = line.strip()
            if nm and nm not in existing:
                st.session_state.setdefault("test_manual_riders", []).append(
                    {"name": nm, "team": "Manuel", "hrmax": 185,
                     "weight": manual_w, "cp": None})
                existing.add(nm)
                added += 1
        if added:
            st.rerun()
        else:
            st.info("Rien à ajouter (vide ou déjà présent).")

    manual_now = st.session_state.get("test_manual_riders", [])
    if manual_now:
        rem = st.multiselect("Retirer des coureurs ajoutés manuellement",
                             [m["name"] for m in manual_now],
                             key="test_manual_remove")
        if st.button("🗑️ Retirer ces coureurs") and rem:
            st.session_state["test_manual_riders"] = [
                m for m in manual_now if m["name"] not in rem]
            for nm in rem:
                st.session_state.pop(f"lac_steps_{nm}", None)
                st.session_state.pop(f"lac_ed_{nm}", None)
                st.session_state.get("sv1_data", {}).pop(nm, None)
            st.rerun()

manual = st.session_state.get("test_manual_riders", [])
known = {str(r.get("name")) for r in riders}
riders = riders + [m for m in manual if str(m.get("name")) not in known]

if not riders:
    st.info("Aucune donnée coureur — choisis une source dans la barre latérale "
            "ou ajoute des coureurs manuellement ci-dessus.")
    st.stop()

names = [str(r.get("name")) for r in riders]
prev_sel = [n for n in st.session_state.get("test_sel", []) if n in names]
sel = st.multiselect(f"Coureurs du test ({len(names)} disponibles)", names,
                     default=prev_sel or names[: min(12, len(names))],
                     max_selections=40)
st.session_state["test_sel"] = sel
if not sel:
    st.info("Sélectionne au moins un coureur.")
    st.stop()

saved_groups = st.session_state.get("test_group_of", {})
grp_key = f"test_groups_{abs(hash(tuple(sel))) % 10 ** 8}"
base_grp = pd.DataFrame({"Coureur": sel,
                         "Groupe": [saved_groups.get(n, GROUPS[0]) for n in sel]})
grp_df = st.data_editor(base_grp, key=grp_key, hide_index=True, use_container_width=True,
                        column_config={
                            "Coureur": st.column_config.TextColumn("Coureur", disabled=True),
                            "Groupe": st.column_config.SelectboxColumn(
                                "Groupe", options=GROUPS, required=True),
                        })
group_of = {str(row["Coureur"]).strip(): str(row["Groupe"])
            for _, row in grp_df.iterrows() if str(row["Coureur"]).strip()}
st.session_state["test_group_of"] = {**saved_groups, **group_of}

rider_by_name = {str(r.get("name")): r for r in riders}


def weight_of(n):
    try:
        w = float(rider_by_name.get(n, {}).get("weight") or 70.0)
    except (TypeError, ValueError):
        return 70.0
    return w if 30 < w < 120 else 70.0


def _num(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _fmt(v, dec=0, unit=""):
    v = _num(v)
    return "—" if v is None else f"{v:.{dec}f}{unit}"


def analyze_rider(n, steps, method, force=None):
    s1 = sv1(steps, method)
    s2 = sv2(steps)
    p_sv1, hr_sv1 = s1 if s1 else (None, None)
    p_sv2, hr_sv2 = s2 if s2 else (None, None)
    if force:
        p_sv1 = force.get("p_sv1") or p_sv1
        hr_sv1 = force.get("hr_sv1") if force.get("hr_sv1") else hr_sv1
        p_sv2 = force.get("p_sv2") or p_sv2
        hr_sv2 = force.get("hr_sv2") if force.get("hr_sv2") else hr_sv2
    hrs = [hr for _, hr, _ in steps if not np.isnan(hr)]
    return {"name": n, "group": group_of.get(n, "—"),
            "p_sv1": p_sv1, "hr_sv1": hr_sv1, "p_sv2": p_sv2, "hr_sv2": hr_sv2,
            "wkg_sv2": (p_sv2 / weight_of(n)) if p_sv2 else None,
            "pmax": max(p for p, _, _ in steps),
            "fcmax": max(hrs) if hrs else None,
            "base_la": baseline_lactate(steps), "weight": weight_of(n)}


TOOLS = {
    "lactate": ("🩸 Lactate Lab",
                "Test par paliers → SV1/SV2, zones watts & FC, rapport complet",
                "En usage"),
    "sv1": ("🎯 SV1 — 3×15′",
            "Zone cible en amont, lactates par bloc, analyse coureur et groupe",
            "En usage"),
    "field": ("🥸 Lactate Terrain — seuils & max",
              "Paliers sur le terrain → SV1 (méthode au choix), SV2 (OBLA 4), "
              "P max du dernier palier, puissance révisée, suspects, lecture "
              "automatique",
              "En usage"),
    "cp": ("⏱️ 5′ & 20′ CP",
           "Deux efforts à bloc → CP/W′ à 2 points, 6 zones d'entraînement",
           "En usage"),
    "cp3": ("⚡ 3′ all-out — CP & W′",
            "Un seul effort de 3 min à bloc → CP des 30 dernières secondes, "
            "W′ du travail au-dessus, contrôle du 5′/20′",
            "En usage"),
    "sprint": ("🌀 Sprints & RSA",
               "Sprints max (3–5 × ~10 s) : pic 1″/5″, usure, ratio vs CP · "
               "RSA (10 × 6 s / 30 s) : capacité à répéter (S_dec)",
               "En usage"),
}

tool = st.session_state.get("test_tool", "")
if tool and tool in TOOLS:
    bcol = st.columns([1, 5])
    if bcol[0].button("← Tous les outils"):
        st.session_state["test_tool"] = ""
        st.rerun()
    st.subheader(TOOLS[tool][0])
else:
    st.subheader("🧰 Multi-tools Testing")
    st.caption("Outils prêts à l'emploi — clique sur Ouvrir.")
    items = list(TOOLS.items())
    for r in range(0, len(items), 2):
        cols = st.columns(2)
        for col, (key, (title, desc_v, badge)) in zip(cols, items[r:r + 2]):
            with col.container(border=True):
                st.markdown(f"**{title}**  \n🟢 {badge}")
                st.caption(desc_v)
                if st.button("Ouvrir", key=f"test_open_{key}",
                             use_container_width=True):
                    st.session_state["test_tool"] = key
                    st.rerun()

# ═════════════════════════ TEST LACTATE PAR PALIERS ═════════════════════════
if tool == "lactate":
    st.subheader("Test par paliers — saisie")
    st.caption("Un palier par ligne : puissance tenue, FC moyenne, lactate capillaire. "
               "Laisse vide ce qui n'a pas été mesuré.")
    method = st.radio("Méthode SV1", list(SV1_METHODS), horizontal=True,
                      help="SV2 = 4 mmol/L (OBLA) dans tous les cas. "
                           "« base » = lactate du palier le plus bas.")
    rider_sel = st.selectbox("Coureur à saisir", sel, key="lac_rider")
    steps_key = f"lac_steps_{rider_sel}"
    if steps_key not in st.session_state:
        st.session_state[steps_key] = pd.DataFrame(
            {"Puissance (W)": [100, 150, 200, 250, 300, 350],
             "FC (bpm)": [None] * 6, "Lactate (mmol/L)": [None] * 6,
             "RPE (/10)": [None] * 6})
    st.caption("Tape tes paliers puis clique sur 💾 Enregistrer ou directement "
               "sur 📊 Générer le rapport — rien n'est perdu pendant "
               "la frappe.")
    try:
        with st.form(f"lac_form_{abs(hash(rider_sel)) % 10 ** 8}",
                     clear_on_submit=False):
            edited = st.data_editor(st.session_state[steps_key],
                                    key=f"lac_ed_{rider_sel}",
                                    hide_index=True, use_container_width=True,
                                    num_rows="dynamic",
                                    column_config={
                                        "Puissance (W)": st.column_config.NumberColumn(
                                            "Puissance (W)", min_value=50,
                                            max_value=1200, step=5),
                                        "FC (bpm)": st.column_config.NumberColumn(
                                            "FC (bpm)", min_value=80, max_value=230,
                                            step=1),
                                        "Lactate (mmol/L)": st.column_config.NumberColumn(
                                            "Lactate (mmol/L)", min_value=0.0,
                                            max_value=20.0, step=0.1, format="%.1f"),
                                        "RPE (/10)": st.column_config.NumberColumn(
                                            "RPE (/10)", min_value=0.0, max_value=10.0,
                                            step=0.5, format="%.1f"),
                                    })
            b1, b2 = st.columns(2)
            saved = b1.form_submit_button("💾 Enregistrer les paliers")
            gen = b2.form_submit_button("📊 Générer le rapport",
                                        type="primary")
    except Exception:
        st.session_state[steps_key] = pd.DataFrame(
            {"Puissance (W)": [None], "FC (bpm)": [None], "Lactate (mmol/L)": [None],
             "RPE (/10)": [None]})
        st.warning("Tableau réinitialisé (état incompatible détecté).")
        st.rerun()
    if saved or gen:
        st.session_state[steps_key] = edited
    if gen:
        results = {}
        for n in sel:
            steps = clean_steps(steps_of(n))
            if sum(1 for _, _, la in steps if not np.isnan(la)) < 2:
                continue
            results[n] = analyze_rider(n, steps, method)
        st.session_state["lac_results"] = results
        if not results:
            st.warning("Aucun palier lactate trouvé : il faut au moins 2 paliers avec "
                       "une valeur de lactate saisie. Vérifie que tu as bien cliqué "
                       "sur « Générer le rapport » dans le formulaire du "
                       "coureur concerné.")

    up_lac = st.file_uploader("Importer des paliers saisis précédemment "
                              "(CSV « données brutes »)", type=["csv"], key="lac_import")
    if up_lac is not None:
        try:
            imp = pd.read_csv(io.StringIO(up_lac.getvalue().decode("utf-8-sig")),
                              sep=None, engine="python", dtype=str)
            imp.columns = [str(c).strip().lower() for c in imp.columns]
            by_name = {}
            for _, row in imp.iterrows():
                nm = str(row.get("coureur", "")).strip()
                if not nm or nm.lower() == "nan":
                    continue

                def col(*keys, _row=row):
                    for k in keys:
                        if k in _row.index:
                            v = _num(str(_row[k]).replace(",", "."))
                            if v is not None:
                                return v
                    return None

                p = col("puissance (w)", "puissance")
                if not p:
                    continue
                by_name.setdefault(nm, []).append(
                    (p, col("fc (bpm)", "fc"), col("lactate (mmol/l)", "lactate")))
            for nm, rows_i in by_name.items():
                st.session_state[f"lac_steps_{nm}"] = pd.DataFrame(
                    rows_i, columns=["Puissance (W)", "FC (bpm)", "Lactate (mmol/L)"])
                st.session_state.pop(f"lac_ed_{nm}", None)
            if by_name:
                st.success(f"{sum(len(v) for v in by_name.values())} palier(s) importé(s) "
                           f"pour {len(by_name)} coureur(s).")
                st.rerun()
        except Exception as e:
            st.error(f"Import impossible : {type(e).__name__} — {e}")

    def steps_of(n):
        df = st.session_state.get(f"lac_steps_{n}")
        if df is None or df.empty:
            return []
        return [(row["Puissance (W)"], row["FC (bpm)"], row["Lactate (mmol/L)"])
                for _, row in df.iterrows()]

    def rpes_of(n):
        df = st.session_state.get(f"lac_steps_{n}")
        if df is None or df.empty or "RPE (/10)" not in df.columns:
            return []
        return [_num(_r.get("RPE (/10)")) for _, _r in df.iterrows()]

    st.caption("✅ Test SV1 minimal : 3 à 4 paliers suffisent. Avec la méthode "
               "« base + 0,5 » ou « base + 1,0 », pas besoin d'atteindre "
               "4 mmol/L — le rapport donne alors des zones provisoires ancrées "
               "sur SV1 seule.")

    results = st.session_state.get("lac_results", {})
    if results:
        for n, res in results.items():
            zw = zone_table(res["p_sv1"], res["p_sv2"])
            zh = zone_table(res["hr_sv1"], res["hr_sv2"])
            wkg = f"{res['wkg_sv2']:.1f} W/kg" if res["wkg_sv2"] else "—"
            with st.container(border=True):
                st.markdown(
                    f"### 🩸 {n} — {res['group']}\n"
                    f"**SV1 : {_fmt(res['p_sv1'])} W / {_fmt(res['hr_sv1'])} bpm** · "
                    f"**SV2 : {_fmt(res['p_sv2'])} W ({wkg}) / {_fmt(res['hr_sv2'])} bpm** · "
                    f"Pmax test {_fmt(res['pmax'])} W · "
                    f"FCmax observée {_fmt(res['fcmax'])} bpm")
                if not res["p_sv2"] and res["p_sv1"]:
                    st.warning("SV2 (4 mmol/L) non atteinte pendant le test — zones "
                               "provisoires ancrées sur SV1. Ajoute un ou deux paliers "
                               "plus durs, ou fixe SV2 à la main ci-dessous.")
                if res["p_sv1"] and res["pmax"] and res["p_sv1"] > 0.85 * res["pmax"]:
                    st.warning("SV1 à plus de 85 % de la Pmax du test : paliers trop "
                               "espacés ou test trop court — SV1 peu fiable, "
                               "refais le test avec des paliers plus fins.")
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**Zones puissance (W)**")
                    ztab = zw or zone_table_partial(res["p_sv1"])
                    if ztab:
                        st.dataframe(pd.DataFrame(
                            [{"Zone": z, "Bas (W)": round(lo) if lo else "—",
                              "Haut (W)": round(hi) if hi else "ouvert"}
                             for z, lo, hi in ztab]), hide_index=True, use_container_width=True)
                    else:
                        st.info("Seuils incomplets — complète les lactates ou ajuste à la main.")
                with c2:
                    st.markdown("**Zones fréquence cardiaque (bpm)**")
                    zhtab = zh or zone_table_partial(res["hr_sv1"])
                    if zhtab:
                        st.dataframe(pd.DataFrame(
                            [{"Zone": z, "Bas (bpm)": round(lo) if lo else "—",
                              "Haut (bpm)": round(hi) if hi else "ouvert"}
                             for z, lo, hi in zhtab]), hide_index=True,
                            use_container_width=True)
                    else:
                        st.info("FC manquantes aux paliers — zones FC indisponibles.")
                steps_c = clean_steps(steps_of(n))
                pts_la = [(p, la) for p, _, la in steps_c if not np.isnan(la)]
                pts_hr = [(p, hr) for p, hr, _ in steps_c if not np.isnan(hr)]
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=[p for p, _ in pts_la], y=[la for _, la in pts_la],
                                         mode="lines+markers", name="Lactate (mmol/L)",
                                         line=dict(color="#dc2626", width=2)))
                if pts_hr:
                    fig.add_trace(go.Scatter(x=[p for p, _ in pts_hr], y=[hr for _, hr in pts_hr],
                                             mode="lines+markers", name="FC (bpm)",
                                             yaxis="y2", line=dict(color="#2563eb", width=1.5)))
                for x, lbl, col in [(res["p_sv1"], "SV1", "#16a34a"),
                                    (res["p_sv2"], "SV2", "#dc2626")]:
                    if x:
                        fig.add_vline(x=x, line_dash="dot", line_color=col,
                                      annotation_text=lbl)
                fig.update_layout(template="plotly_white", height=340, hovermode="x unified",
                                  xaxis_title="Puissance (W)", yaxis_title="Lactate (mmol/L)",
                                  yaxis2=dict(overlaying="y", side="right", title="FC (bpm)",
                                              showgrid=False),
                                  legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                              xanchor="right", x=1))
                st.plotly_chart(fig, use_container_width=True)
                _rc_ = clean_steps(steps_of(n))
                _rp_ = rpes_of(n)
                if _rp_ and any(v is not None for v in _rp_):
                    st.markdown("**Paliers & RPE (ressenti)**")
                    st.dataframe(pd.DataFrame(
                        [{"Palier": i + 1,
                          "Puissance (W)": None if np.isnan(p) else p,
                          "RPE (/10)": v}
                         for i, (v, (p, _, _)) in enumerate(zip(_rp_, _rc_))
                         if v is not None]),
                        hide_index=True, use_container_width=True)

        with st.expander(f"✍️ Ajuster les seuils à la main — {rider_sel}"):
            cur = results.get(rider_sel)
            if not cur:
                st.info("Génère d'abord le rapport pour ce coureur.")
            else:
                oc1, oc2 = st.columns(2)
                p1 = oc1.number_input("P SV1 (W)", 50, 800, int(cur["p_sv1"] or 0), 5,
                                      key="ov_p1")
                h1 = oc2.number_input("FC SV1 (bpm)", 80, 230, int(cur["hr_sv1"] or 0), 1,
                                      key="ov_h1")
                oc3, oc4 = st.columns(2)
                p2 = oc3.number_input("P SV2 (W)", 50, 900, int(cur["p_sv2"] or 0), 5,
                                      key="ov_p2")
                h2 = oc4.number_input("FC SV2 (bpm)", 80, 230, int(cur["hr_sv2"] or 0), 1,
                                      key="ov_h2")
                if st.button("Appliquer et recalculer les zones"):
                    results[rider_sel] = analyze_rider(
                        rider_sel, clean_steps(steps_of(rider_sel)), method,
                        force={"p_sv1": p1 or None, "hr_sv1": h1 or None,
                               "p_sv2": p2 or None, "hr_sv2": h2 or None})
                    st.session_state["lac_results"] = results
                    st.rerun()

        zc1, zc2, zc3 = st.columns(3)
        _rpe_moy = {}
        for _rn in results:
            _rv = [v for v in rpes_of(_rn) if v is not None]
            _rpe_moy[_rn] = round(float(np.mean(_rv)), 1) if _rv else None
        seuils_csv = pd.DataFrame(
            [{"Coureur": r["name"], "Groupe": r["group"],
              "RPE moy. (/10)": _rpe_moy.get(r["name"]),
              "P SV1 (W)": round(r["p_sv1"]) if r["p_sv1"] else "",
              "FC SV1": round(r["hr_sv1"]) if _num(r["hr_sv1"]) else "",
              "P SV2 (W)": round(r["p_sv2"]) if r["p_sv2"] else "",
              "FC SV2": round(r["hr_sv2"]) if _num(r["hr_sv2"]) else "",
              "W/kg SV2": round(r["wkg_sv2"], 2) if r["wkg_sv2"] else "",
              "Pmax (W)": round(r["pmax"]),
              "FCmax": round(r["fcmax"]) if _num(r["fcmax"]) else ""}
             for r in results.values()]).to_csv(index=False, sep=";", decimal=",")
        zc1.download_button("⬇️ Seuils & W/kg (CSV)", seuils_csv.encode("utf-8-sig"),
                            "testing_seuils.csv", "text/csv")
        zone_rows = []
        for r in results.values():
            zw = zone_table(r["p_sv1"], r["p_sv2"]) or []
            zh = {z: (lo, hi)
                  for z, lo, hi in (zone_table(r["hr_sv1"], r["hr_sv2"]) or [])}
            for z, lo, hi in zw:
                llo, lhi = zh.get(z, (None, None))
                zone_rows.append(
                    {"Coureur": r["name"], "Groupe": r["group"], "Zone": z,
                     "Bas (W)": round(lo) if lo else "",
                     "Haut (W)": round(hi) if hi else "",
                     "Bas (bpm)": round(llo) if llo else "",
                     "Haut (bpm)": round(lhi) if lhi else ""})
        zones_csv = pd.DataFrame(zone_rows).to_csv(index=False, sep=";", decimal=",")
        zc2.download_button("⬇️ Zones (CSV)", zones_csv.encode("utf-8-sig"),
                            "testing_zones.csv", "text/csv")
        raw_rows = [{"Coureur": n, "Groupe": group_of.get(n, "—"), "Palier": i + 1,
                     "Puissance (W)": p, "FC (bpm)": "" if np.isnan(hr) else hr,
                     "Lactate (mmol/L)": "" if np.isnan(la) else la}
                    for n in sel for i, (p, hr, la) in enumerate(clean_steps(steps_of(n)))]
        raw_csv = pd.DataFrame(raw_rows).to_csv(index=False, sep=";", decimal=",")
        zc3.download_button("⬇️ Données brutes (CSV)", raw_csv.encode("utf-8-sig"),
                            "testing_paliers_bruts.csv", "text/csv")

# ═════════════════════════ TEST SV1 — 3 × 15' ═════════════════════════
elif tool == "sv1":
    st.subheader("1 · Zone de travail — calage des blocs de 15'")
    st.caption("3 × 15' en zone entre SV1 et SV2. PLANCHER (W) = bas de la zone — "
               "la puissance au niveau de SV1. PLAFOND (W) = haut de la zone — à "
               "mi-chemin entre SV1 et SV2. Le coureur roule entre les deux, FC "
               "entre FC plancher et FC plafond. Sans test lactate saisi : "
               "estimation 78–85 % FCmax et 70–85 % CP. Tout est modifiable à la main.")
    lac_res = st.session_state.get("lac_results", {})
    tgt_rows = []
    for n in sel:
        r = rider_by_name.get(n, {})
        try:
            hrmax = float(r.get("hrmax") or 185)
        except (TypeError, ValueError):
            hrmax = 185.0
        cp = _num(r.get("cp")) or _num(r.get("p60m"))
        lr = lac_res.get(n)
        if lr and lr.get("p_sv1"):
            lo_hr = lr["hr_sv1"] + 2 if _num(lr["hr_sv1"]) else 0.78 * hrmax
            hi_hr = lr["hr_sv2"] - 10 if _num(lr["hr_sv2"]) else 0.85 * hrmax
            lo_p = lr["p_sv1"]
            hi_p = (lr["p_sv1"] + lr["p_sv2"]) / 2 if lr.get("p_sv2") else lr["p_sv1"] * 1.10
            base = "mesuré au test lactate"
        else:
            lo_hr, hi_hr = 0.78 * hrmax, 0.85 * hrmax
            lo_p = 0.70 * cp if cp else None
            hi_p = 0.85 * cp if cp else None
            base = "estimé sans test lactate (%FCmax / %CP)"
        tgt_rows.append({"Coureur": n, "Groupe": group_of.get(n, "—"),
                         "FC plancher": round(lo_hr), "FC plafond": round(hi_hr),
                         "Plancher (W)": round(lo_p) if lo_p else None,
                         "Plafond (W)": round(hi_p) if hi_p else None,
                         "Source des cibles": base})
    tgt_key = f"sv1_targets_{abs(hash(tuple(sel))) % 10 ** 8}"
    tdf = st.data_editor(pd.DataFrame(tgt_rows), key=tgt_key, hide_index=True,
                         use_container_width=True, num_rows="dynamic",
                         column_config={
                             "Coureur": st.column_config.TextColumn("Coureur", disabled=True),
                             "Groupe": st.column_config.TextColumn("Groupe", disabled=True),
                             "FC plancher": st.column_config.NumberColumn(
                                 "FC plancher (bpm)", min_value=80, max_value=230),
                             "FC plafond": st.column_config.NumberColumn(
                                 "FC plafond (bpm)", min_value=80, max_value=230),
                             "Plancher (W)": st.column_config.NumberColumn(
                                 "Plancher (W)", min_value=50, max_value=700,
                                 help="Bas de la zone des blocs de 15' — niveau SV1."),
                             "Plafond (W)": st.column_config.NumberColumn(
                                 "Plafond (W)", min_value=50, max_value=700,
                                 help="Haut de la zone — mi-chemin SV1→SV2."),
                             "Source des cibles": st.column_config.TextColumn(
                                 "Source", disabled=True,
                                 help="D'où viennent les chiffres pour ce coureur."),
                         })
    st.download_button("⬇️ Cibles 3×15' (CSV)",
                       tdf.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       "cibles_sv1_3x15.csv", "text/csv")

    st.subheader("2 · Mesures prises pendant la séance")
    st.caption("Par bloc : TEMPS RÉEL (cible 15'), lactate (piqûre juste après), "
               "lactate de secours si piqûre douteuse (la médiane est retenue — "
               "aucune donnée perdue), n° de l'appareil, FC moyenne, puissance "
               "moyenne (compteur), distance, RPE et commentaire. Le lactate de "
               "repos sert de base. Enregistre par coureur — les valeurs déjà "
               "saisies sont reprises au chargement.")
    _SV1_EDIT_KEYS = ("sv1_lr", "sv1_la1", "sv1_la2", "sv1_la3",
                      "sv1_l2b1", "sv1_l2b2", "sv1_l2b3",
                      "sv1_tm1", "sv1_tm2", "sv1_tm3",
                      "sv1_fc1", "sv1_fc2", "sv1_fc3",
                      "sv1_pw1", "sv1_pw2", "sv1_pw3",
                      "sv1_km1", "sv1_km2", "sv1_km3",
                      "sv1_rp1", "sv1_rp2", "sv1_rp3",
                      "sv1_dv1", "sv1_dv2", "sv1_dv3",
                      "sv1_c1", "sv1_c2", "sv1_c3")
    try:
        sv1_rider = st.pills("Coureur — un clic", sel, key="sv1_rider",
                             default=[sel[0]],
                             help="Plusieurs coureurs en même temps ? Passe "
                                  "de l'un à l'autre en un clic — les valeurs "
                                  "non enregistrées de l'un ne fuient jamais "
                                  "chez l'autre.")
    except Exception:
        sv1_rider = st.selectbox("Coureur", sel, key="sv1_rider")
    if not sv1_rider:
        sv1_rider = sel[0]
    _prev_v = st.session_state.get("sv1_rider_prev")
    if _prev_v is not None and _prev_v != sv1_rider:
        for _k in _SV1_EDIT_KEYS:
            st.session_state.pop(_k, None)
    st.session_state["sv1_rider_prev"] = sv1_rider

    def _med_lac(a, b):
        """Lactate retenu : médiane des mesures (1 ou 2) — une pique ratée
        ne fausse plus le bloc."""
        if a is None:
            return None
        if b is None or b == 0:
            return float(a)
        return float(np.median([float(a), float(b)]))

    _prev_sv1 = st.session_state.get("sv1_data", {}).get(sv1_rider, {})
    _prev_p = _prev_sv1.get("p") or [None, None, None]
    _prev_km = _prev_sv1.get("km") or [None, None, None]
    _prev_rpe = _prev_sv1.get("rpe") or [None, None, None]
    _prev_lr = _prev_sv1.get("la_repos")
    _prev_tm = _prev_sv1.get("tm") or [None, None, None]
    _prev_l2 = _prev_sv1.get("la_sec") or [None, None, None]
    _prev_dev = _prev_sv1.get("dev") or ["", "", ""]
    _prev_cmt = _prev_sv1.get("cmt") or ["", "", ""]
    with st.form("sv1_entry"):
        st.markdown(f"**{sv1_rider}** — le lactate de repos sert de base")
        _lr = st.number_input("Lactate de repos (mmol/L)", 0.0, 20.0,
                              float(_prev_lr) if _prev_lr else 0.8, 0.1,
                              format="%.1f", key="sv1_lr")
        _hcols = st.columns(3)
        for _i, _h in enumerate(["Bloc 1", "Bloc 2", "Bloc 3"]):
            _hcols[_i].caption(_h)
        st.caption("Le coureur s'arrête devant toi : note le TEMPS RÉEL de chaque "
                   "bloc (cible 15') — un bloc trop court ou trop long doit se "
                   "voir. Piqûre douteuse ? note un « lactate de secours » (la "
                   "MÉDIANE est retenue) et le n° de l'appareil (plusieurs "
                   "techniciens : si un appareil déraille, on sait quelles piqûres "
                   "revoir).")
        _tm_c = st.columns(3)
        tm1 = _tm_c[0].number_input("Temps réel bloc 1 (min)", 1.0, 90.0,
                                    float(_prev_tm[0]) if _prev_tm[0] else 15.0,
                                    0.5, format="%.1f", key="sv1_tm1",
                                    help="Temps CHRONOMÉTRÉ du bloc, pas la cible "
                                         "théorique : un bloc mal géré doit se voir.")
        tm2 = _tm_c[1].number_input("Temps réel bloc 2 (min)", 1.0, 90.0,
                                    float(_prev_tm[1]) if _prev_tm[1] else 15.0,
                                    0.5, format="%.1f", key="sv1_tm2")
        tm3 = _tm_c[2].number_input("Temps réel bloc 3 (min)", 1.0, 90.0,
                                    float(_prev_tm[2]) if _prev_tm[2] else 15.0,
                                    0.5, format="%.1f", key="sv1_tm3")
        _la_c = st.columns(3)
        la1 = _la_c[0].number_input("Lactate bloc 1 (mmol/L)", 0.0, 20.0, 1.5, 0.1,
                                    format="%.1f", key="sv1_la1")
        la2 = _la_c[1].number_input("Lactate bloc 2 (mmol/L)", 0.0, 20.0, 1.5, 0.1,
                                    format="%.1f", key="sv1_la2")
        la3 = _la_c[2].number_input("Lactate bloc 3 (mmol/L)", 0.0, 20.0, 1.5, 0.1,
                                    format="%.1f", key="sv1_la3")
        _l2_c = st.columns(3)
        l2b1 = _l2_c[0].number_input("Lactate de secours bloc 1", 0.0, 20.0,
                                     float(_prev_l2[0]) if _prev_l2[0] else 0.0,
                                     0.1, format="%.1f", key="sv1_l2b1",
                                     help="2ᵉ piqûre en cas de doute — 0 = pas de "
                                          "2ᵉ piqûre ; la médiane est retenue.")
        l2b2 = _l2_c[1].number_input("Lactate de secours bloc 2", 0.0, 20.0,
                                     float(_prev_l2[1]) if _prev_l2[1] else 0.0,
                                     0.1, format="%.1f", key="sv1_l2b2")
        l2b3 = _l2_c[2].number_input("Lactate de secours bloc 3", 0.0, 20.0,
                                     float(_prev_l2[2]) if _prev_l2[2] else 0.0,
                                     0.1, format="%.1f", key="sv1_l2b3")
        _dv_c = st.columns(3)
        dv1 = _dv_c[0].text_input("N° appareil bloc 1", _prev_dev[0] or "",
                                  key="sv1_dv1",
                                  help="Plusieurs techniciens : si un appareil "
                                       "déraille, on sait quelles piqûres revoir.")
        dv2 = _dv_c[1].text_input("N° appareil bloc 2", _prev_dev[1] or "",
                                  key="sv1_dv2")
        dv3 = _dv_c[2].text_input("N° appareil bloc 3", _prev_dev[2] or "",
                                  key="sv1_dv3")
        _fc_c = st.columns(3)
        fc1 = _fc_c[0].number_input("FC moy. bloc 1 (bpm)", 80, 230, 150,
                                    key="sv1_fc1")
        fc2 = _fc_c[1].number_input("FC moy. bloc 2 (bpm)", 80, 230, 150,
                                    key="sv1_fc2")
        fc3 = _fc_c[2].number_input("FC moy. bloc 3 (bpm)", 80, 230, 150,
                                    key="sv1_fc3")
        _pw_c = st.columns(3)
        pw1 = _pw_c[0].number_input("P moy. bloc 1 (W)", 0, 800,
                                    int(_prev_p[0]) if _prev_p[0] else 0,
                                    key="sv1_pw1")
        pw2 = _pw_c[1].number_input("P moy. bloc 2 (W)", 0, 800,
                                    int(_prev_p[1]) if _prev_p[1] else 0,
                                    key="sv1_pw2")
        pw3 = _pw_c[2].number_input("P moy. bloc 3 (W)", 0, 800,
                                    int(_prev_p[2]) if _prev_p[2] else 0,
                                    key="sv1_pw3")
        _km_c = st.columns(3)
        km1 = _km_c[0].number_input("Distance bloc 1 (km)", 0.0, 100.0,
                                    float(_prev_km[0]) if _prev_km[0] else 0.0,
                                    0.1, format="%.1f", key="sv1_km1")
        km2 = _km_c[1].number_input("Distance bloc 2 (km)", 0.0, 100.0,
                                    float(_prev_km[1]) if _prev_km[1] else 0.0,
                                    0.1, format="%.1f", key="sv1_km2")
        km3 = _km_c[2].number_input("Distance bloc 3 (km)", 0.0, 100.0,
                                    float(_prev_km[2]) if _prev_km[2] else 0.0,
                                    0.1, format="%.1f", key="sv1_km3")
        _rp_c = st.columns(3)
        rpe1 = _rp_c[0].number_input("RPE bloc 1 (/10)", 0.0, 10.0,
                                     float(_prev_rpe[0]) if _prev_rpe[0] else 0.0,
                                     0.5, format="%.1f", key="sv1_rp1")
        rpe2 = _rp_c[1].number_input("RPE bloc 2 (/10)", 0.0, 10.0,
                                     float(_prev_rpe[1]) if _prev_rpe[1] else 0.0,
                                     0.5, format="%.1f", key="sv1_rp2")
        rpe3 = _rp_c[2].number_input("RPE bloc 3 (/10)", 0.0, 10.0,
                                     float(_prev_rpe[2]) if _prev_rpe[2] else 0.0,
                                     0.5, format="%.1f", key="sv1_rp3")
        _cm_c = st.columns(3)
        c1 = _cm_c[0].text_input("Commentaire bloc 1", _prev_cmt[0] or "",
                                 placeholder="ex. piqûre ratée, vent côté",
                                 key="sv1_c1")
        c2 = _cm_c[1].text_input("Commentaire bloc 2", _prev_cmt[1] or "",
                                 key="sv1_c2")
        c3 = _cm_c[2].text_input("Commentaire bloc 3", _prev_cmt[2] or "",
                                 key="sv1_c3")
        if st.form_submit_button("💾 Enregistrer ces données"):
            st.session_state.setdefault("sv1_data", {})[sv1_rider] = {
                "la": [_med_lac(la1, l2b1), _med_lac(la2, l2b2),
                       _med_lac(la3, l2b3)],
                "la_sec": [l2b1 or None, l2b2 or None, l2b3 or None],
                "tm": [tm1 or None, tm2 or None, tm3 or None],
                "fc": [fc1, fc2, fc3],
                "p": [pw1 or None, pw2 or None, pw3 or None],
                "km": [km1 or None, km2 or None, km3 or None],
                "rpe": [rpe1 or None, rpe2 or None, rpe3 or None],
                "dev": [dv1 or "", dv2 or "", dv3 or ""],
                "cmt": [c1 or "", c2 or "", c3 or ""],
                "la_repos": _lr,
                "group": group_of.get(sv1_rider, "—")}


    data = st.session_state.get("sv1_data", {})
    entered = [n for n in sel if n in data]

    def sv1_global(d):
        las = [la for la in d["la"] if la is not None]
        if not las:
            return "—"
        if max(las) >= 4.0:
            return "🚨 Trop dur — un bloc ≥ 4 mmol/L, réduis la cible"
        trend = sv1_trend(d["la"])
        if trend and trend[0].startswith("dérive franche"):
            return "⚠️ Cible trop haute — dérive franche du lactate"
        if np.mean(las) < 1.2:
            return "🟦 Très en dessous de SV1 — intensité sûre, peut-être trop facile"
        if trend and trend[0].startswith("légère dérive"):
            return "🟡 Limite — cible un peu haute"
        return "✅ Zone bien calée"

    if entered:
        st.subheader("3 · Analyse coureur par coureur")
        st.dataframe(pd.DataFrame(
            [{"Coureur": n, "Groupe": data[n]["group"],
              "Temps (min)": " / ".join(
                  f"{x:.1f}" if x else "—"
                  for x in (data[n].get("tm") or [None] * 3)),
              "Appareils": " / ".join(data[n].get("dev") or ["", "", ""]),
              "B1": data[n]["la"][0], "B2": data[n]["la"][1], "B3": data[n]["la"][2],
              "Dérive B1→B3": f"{data[n]['la'][2] - data[n]['la'][0]:+.1f}",
              "Commentaire": " / ".join(c for c in (data[n].get("cmt") or [])
                                        if c),
              "Verdict": sv1_global(data[n])} for n in entered]),
            hide_index=True, use_container_width=True)
        def _tgt_of(rider):
            tdf = st.session_state.get(tgt_key)
            if isinstance(tdf, pd.DataFrame) and not tdf.empty:
                for _, trow in tdf.iterrows():
                    if str(trow.get("Coureur", "")).strip() == rider:
                        return trow
            return None

        for n in entered:
            d = data[n]
            trend = sv1_trend(d["la"])
            with st.expander(f"{n} — {d['group']}"):
                if d.get("la_repos") is not None:
                    st.caption(f"Lactate de repos : {d['la_repos']:.1f} mmol/L")
                _trow = _tgt_of(n)
                if _trow is not None:
                    _fcb = _num(_trow.get("FC plancher"))
                    _fcp = _num(_trow.get("FC plafond"))
                    _pln = _num(_trow.get("Plancher (W)"))
                    _pfl = _num(_trow.get("Plafond (W)"))
                    _src2 = str(_trow.get("Source des cibles") or "")
                    _ct = "🎯 Cible donnée au coureur :"
                    if _fcb and _fcp:
                        _ct += f" FC {_fcb:.0f}–{_fcp:.0f} bpm"
                    if _pln and _pfl:
                        _ct += f" · {_pln:.0f}–{_pfl:.0f} W"
                    if _src2:
                        _ct += f" — {_src2}"
                    st.caption(_ct)
                for i in range(3):
                    la_i = d["la"][i]
                    fc_i = d["fc"][i]
                    p_i = (d.get("p") or [None] * 3)[i]
                    km_i = (d.get("km") or [None] * 3)[i]
                    rpe_i = (d.get("rpe") or [None] * 3)[i]
                    tm_i = (d.get("tm") or [None] * 3)[i]
                    dev_i = (d.get("dev") or ["", "", ""])[i]
                    cmt_i = (d.get("cmt") or ["", "", ""])[i]
                    bits = [f"lactate **{la_i:.1f} mmol/L** — "
                            f"{sv1_block_verdict(la_i)}",
                            f"FC moy. {fc_i:.0f} bpm"]
                    if p_i:
                        bits.append(f"P moy. {p_i:.0f} W "
                                    f"({p_i / weight_of(n):.1f} W/kg)")
                    if km_i:
                        bits.append(f"{km_i:.1f} km")
                    if tm_i:
                        bits.append(f"{tm_i:.1f} min")
                    if dev_i:
                        bits.append(f"appareil n°{dev_i}")
                    if rpe_i:
                        bits.append(f"RPE {rpe_i:.0f}/10")
                    if tm_i and abs(tm_i - 15.0) >= 1.0:
                        st.caption("🟠 Bloc mal géré : " +
                                   ("trop court — " if tm_i < 15.0
                                    else "trop long — " )
                                   + f"{tm_i:.1f} min au lieu de 15'")
                    if cmt_i:
                        st.caption(f"💬 {cmt_i}")
                    st.markdown(f"**Bloc {i + 1}** : " + " · ".join(bits))
                    if _trow is not None:
                        lo_f = _num(_trow.get("FC plancher"))
                        hi_f = _num(_trow.get("FC plafond"))
                        if lo_f and hi_f and not (lo_f - 3 <= fc_i <= hi_f + 2):
                            st.caption("🟠 FC hors fenêtre cible "
                                       f"({lo_f:.0f}–{hi_f:.0f} bpm)")
                        pl = _num(_trow.get("Plancher (W)"))
                        ph = _num(_trow.get("Plafond (W)"))
                        if pv_al := _num(p_i):
                            if pl and ph and not (pl - 12 <= pv_al <= ph + 12):
                                st.caption("🟠 Puissance hors fenêtre cible "
                                           f"({pl:.0f}–{ph:.0f} W)")
                if trend:
                    st.info(f"**{trend[0].capitalize()}** — {trend[1]}")
                st.markdown(f"**Bilan :** {sv1_global(d)}")

        st.subheader("4 · Analyse par groupe")
        grp_choice = st.selectbox("Groupe", ["Tous les groupes"] + GROUPS, key="sv1_grp")
        pool = [n for n in entered
                if grp_choice == "Tous les groupes" or data[n]["group"] == grp_choice]
        if pool:
            agg = []
            for g in sorted({data[n]["group"] for n in pool}):
                members = [n for n in pool if data[n]["group"] == g]
                las = np.array([data[n]["la"] for n in members], dtype=float)

                def _bmean(key, idx):
                    vals = [(data[n].get(key) or [None] * 3)[idx] for n in members]
                    vals = [float(v) for v in vals if v is not None and np.isfinite(v)]
                    return round(float(np.mean(vals)), 1) if vals else None

                agg.append({"Groupe": g, "Coureurs": len(members),
                            "Lactate moy. B1": round(float(las[:, 0].mean()), 2),
                            "Lactate moy. B2": round(float(las[:, 1].mean()), 2),
                            "Lactate moy. B3": round(float(las[:, 2].mean()), 2),
                            "P moy. B1 (W)": _bmean("p", 0),
                            "P moy. B2 (W)": _bmean("p", 1),
                            "P moy. B3 (W)": _bmean("p", 2),
                            "Temps moy. B1 (min)": _bmean("tm", 0),
                            "Temps moy. B2 (min)": _bmean("tm", 1),
                            "Temps moy. B3 (min)": _bmean("tm", 2),
                            "RPE moy.": _bmean("rpe", 2),
                            "Dérive moy.": round(float((las[:, 2] - las[:, 0]).mean()), 2),
                            "Bloc(s) ≥ 4": int((las >= 4.0).sum()),
                            "Bloc(s) < 1,5": int((las < 1.5).sum())})
            st.dataframe(pd.DataFrame(agg), hide_index=True, use_container_width=True)
            fig = go.Figure()
            for a in agg:
                fig.add_trace(go.Bar(name=a["Groupe"],
                                     x=["Bloc 1", "Bloc 2", "Bloc 3"],
                                     y=[a["Lactate moy. B1"], a["Lactate moy. B2"],
                                        a["Lactate moy. B3"]]))
            fig.add_hline(y=2.0, line_dash="dot", line_color="#d97706",
                          annotation_text="SV1 (2 mmol/L)")
            fig.add_hline(y=4.0, line_dash="dot", line_color="#dc2626",
                          annotation_text="SV2 (4 mmol/L)")
            fig.update_layout(template="plotly_white", barmode="group", height=340,
                              yaxis_title="Lactate moyen (mmol/L)",
                              legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                          xanchor="right", x=1))
            st.plotly_chart(fig, use_container_width=True)
            st.download_button("⬇️ Résultats SV1 (CSV)",
                               pd.DataFrame([{"Coureur": n,
                                              "Groupe": data[n]["group"],
                                              "Lactate repos": data[n].get("la_repos"),
                                              "Lactate B1": data[n]["la"][0],
                                              "Lactate B2": data[n]["la"][1],
                                              "Lactate B3": data[n]["la"][2],
                                              "FC moy. B1": data[n]["fc"][0],
                                              "FC moy. B2": data[n]["fc"][1],
                                              "FC moy. B3": data[n]["fc"][2],
                                              "P moy. B1 (W)": (data[n].get("p") or [None] * 3)[0],
                                              "P moy. B2 (W)": (data[n].get("p") or [None] * 3)[1],
                                              "P moy. B3 (W)": (data[n].get("p") or [None] * 3)[2],
                                              "Distance B1 (km)": (data[n].get("km") or [None] * 3)[0],
                                              "Distance B2 (km)": (data[n].get("km") or [None] * 3)[1],
                                              "Distance B3 (km)": (data[n].get("km") or [None] * 3)[2],
                                              "Lactate sec. B1": (data[n].get("la_sec") or [None] * 3)[0],
                                              "Lactate sec. B2": (data[n].get("la_sec") or [None] * 3)[1],
                                              "Lactate sec. B3": (data[n].get("la_sec") or [None] * 3)[2],
                                              "Temps réel B1 (min)": (data[n].get("tm") or [None] * 3)[0],
                                              "Temps réel B2 (min)": (data[n].get("tm") or [None] * 3)[1],
                                              "Temps réel B3 (min)": (data[n].get("tm") or [None] * 3)[2],
                                              "Appareil B1": (data[n].get("dev") or ["", "", ""])[0],
                                              "Appareil B2": (data[n].get("dev") or ["", "", ""])[1],
                                              "Appareil B3": (data[n].get("dev") or ["", "", ""])[2],
                                              "Commentaire B1": (data[n].get("cmt") or ["", "", ""])[0],
                                              "Commentaire B2": (data[n].get("cmt") or ["", "", ""])[1],
                                              "Commentaire B3": (data[n].get("cmt") or ["", "", ""])[2],
                                              "RPE B1": (data[n].get("rpe") or [None] * 3)[0],
                                              "RPE B2": (data[n].get("rpe") or [None] * 3)[1],
                                              "RPE B3": (data[n].get("rpe") or [None] * 3)[2],
                                              "Verdict": sv1_global(data[n])}
                                             for n in pool]).to_csv(
                                   index=False, sep=";", decimal=",").encode("utf-8-sig"),
                               "resultats_sv1_3x15.csv", "text/csv")
    else:
        st.info("Enregistre les lactates d'un coureur ci-dessus pour lancer les analyses.")


# ═════════════════════ TEST LACTATE DE TERRAIN — PALIERS RAMPÉS ═════════════════════
elif tool == "field":

    def _round0(v):
        return round(v) if v else None

    def _round1(v):
        return round(v, 1) if v else None

    def _round2(v):
        return round(v, 2) if v else None

    def _palette(row):
        color = ("#fee2e2" if row.get("_niveau") == "rouge"
                 else "#fef3c7" if row.get("_niveau") == "orange" else "")
        return [f"background-color:{color}"] * len(row) if color \
            else [""] * len(row)

    def rev_power(p, fc, fcmax):
        """Formule équipe : P révisée = P − ((FC − 0,8 × FCmax) / FCmax) × P."""
        if not (p and fc and fcmax):
            return None
        return p - ((fc - 0.8 * fcmax) / fcmax) * p

    def field_report_for(n, ft, method):
        """Analyse complète du test terrain d'un coureur : seuils sur la
        puissance révisée, zones, paliers suspects, graphique."""
        grp = ft.get("group") or group_of.get(n, "—")
        paliers_raw = []
        pals = ft.get("paliers")
        if not isinstance(pals, pd.DataFrame):
            return {"error": "aucun palier enregistré"}
        for i, r0 in pals.iterrows():
            p = _num(r0.get("P moy. (W)"))
            if not p:
                continue
            fc = _num(r0.get("FC moy. (bpm)"))
            # Multi-pricks : 1 à 3 lactates par palier — on retient la MÉDIANE
            # (si tendance douteuse sur une piqûre, elle ne fausse plus le seuil).
            _lvs = [x for x in (_num(r0.get("Lactate (mmol/L)")),
                                _num(r0.get("Lactate 2 (mmol/L)")),
                                _num(r0.get("Lactate 3 (mmol/L)")))
                    if x is not None]
            la = (float(np.median(_lvs)) if _lvs else None)
            dur = _num(r0.get("Durée (min)"))
            rpe = _num(r0.get("RPE (/10)"))
            prv = rev_power(p, fc, _num(ft.get("fcmax")))
            paliers_raw.append({"Palier": i + 1, "Durée (min)": dur,
                                "P moy. (W)": p, "FC moy. (bpm)": fc,
                                "Lactate (mmol/L)": la,
                                "Lactates saisis": " + ".join(f"{v:.1f}" for v in _lvs)
                                if len(_lvs) > 1 else "",
                                "RPE": rpe,
                                "P révisée (W)": prv,
                                "Commentaire": str(r0.get("Commentaire") or "")})
        if len([r for r in paliers_raw if r["Lactate (mmol/L)"] is not None]) < 2:
            return {"error": "moins de 2 lactates saisis — seuils impossibles"}
        paliers_raw.sort(key=lambda r: (r["P révisée (W)"] or r["P moy. (W)"]))
        prev_la = None
        for r in paliers_raw:
            flags = []
            p, fc = r["P moy. (W)"], r["FC moy. (bpm)"]
            prv, la = r["P révisée (W)"], r["Lactate (mmol/L)"]
            dur = r["Durée (min)"]
            if prv:
                r["W/kg"] = round(prv / (ft["poids"] or 70.0), 2)
            if fc is not None and _num(ft.get("fcmax")):
                r["% FCmax"] = round(fc / _num(ft.get("fcmax")) * 100)
                if fc >= _num(ft.get("fcmax")):
                    flags.append("FC ≥ FCmax")
            if la is None:
                flags.append("lactate manquant")
            elif prev_la is not None and la < prev_la - 0.4:
                flags.append("courbe inversée")
            elif la >= 6.0:
                flags.append("lactate très élevé")
            if p and prv and abs(prv - p) / p > 0.10:
                flags.append("révision > 10 %")
            if dur is not None and dur < 3:
                flags.append("durée < 3'")
            prev_la = la if la is not None else prev_la
            r["_niveau"] = ("rouge" if ("FC ≥ FCmax" in flags
                                        or "lactate très élevé" in flags)
                            else "orange" if flags else "")
            r["⚠️ Suspect"] = " · ".join(flags)
        steps = [(r["P révisée (W)"] or r["P moy. (W)"],
                  r["FC moy. (bpm)"] if r["FC moy. (bpm)"] is not None else np.nan,
                  r["Lactate (mmol/L)"] if r["Lactate (mmol/L)"] is not None
                  else np.nan) for r in paliers_raw]
        s1, s2 = sv1(steps, method), sv2(steps)
        p_sv1, hr_sv1 = s1 if s1 else (None, None)
        p_sv2, hr_sv2 = s2 if s2 else (None, None)
        zw = (zone_table(p_sv1, p_sv2) if p_sv1 and p_sv2
              else zone_table_partial(p_sv1))
        hr1_ok = hr_sv1 is not None and not (isinstance(hr_sv1, float)
                                             and np.isnan(hr_sv1))
        hr2_ok = hr_sv2 is not None and not (isinstance(hr_sv2, float)
                                             and np.isnan(hr_sv2))
        zh = (zone_table(hr_sv1, hr_sv2) if hr1_ok and hr2_ok
              else zone_table_partial(hr_sv1 if hr1_ok else None))
        revs = [r["P révisée (W)"] or r["P moy. (W)"] for r in paliers_raw]
        wkgs = [r.get("W/kg") for r in paliers_raw if r.get("W/kg")]
        las = [r["Lactate (mmol/L)"] for r in paliers_raw
               if r["Lactate (mmol/L)"] is not None]
        # Graphique : lactate + FC vs puissance révisée
        fig = go.Figure()
        xs = [r["P révisée (W)"] or r["P moy. (W)"] for r in paliers_raw]
        fig.add_trace(go.Scatter(
            x=xs, y=[r["Lactate (mmol/L)"] for r in paliers_raw],
            mode="markers+lines", name="Lactate",
            text=[f"Palier {r['Palier']}" for r in paliers_raw],
            marker=dict(color="#dc2626", size=10),
            line=dict(color="#dc2626"),
            hovertemplate="%{text}<br>%{x:.0f} W · %{y:.1f} mmol/L<extra></extra>"))
        fcs = [(r["P révisée (W)"] or r["P moy. (W)"], r["FC moy. (bpm)"])
               for r in paliers_raw if r["FC moy. (bpm)"] is not None]
        if fcs:
            fig.add_trace(go.Scatter(
                x=[f[0] for f in fcs], y=[f[1] for f in fcs],
                mode="markers+lines", name="FC moy.", yaxis="y2",
                marker=dict(color="#2563eb", size=8),
                line=dict(color="#2563eb", dash="dot"),
                hovertemplate="%{x:.0f} W · %{y:.0f} bpm<extra></extra>"))
        if p_sv1:
            fig.add_vline(x=p_sv1, line_dash="dash", line_color="#d97706",
                          annotation_text=f"SV1 {p_sv1:.0f} W")
        if p_sv2:
            fig.add_vline(x=p_sv2, line_dash="dash", line_color="#dc2626",
                          annotation_text=f"SV2 {p_sv2:.0f} W")
        fig.update_layout(template="plotly_white", height=360,
                          xaxis_title="Puissance révisée (W)",
                          yaxis_title="Lactate (mmol/L)",
                          yaxis2=dict(title="FC moy. (bpm)", overlaying="y",
                                      side="right", range=[80, 230]),
                          legend=dict(orientation="h", yanchor="bottom",
                                      y=1.02, xanchor="right", x=1))
        n_susp = sum(1 for r in paliers_raw if r["_niveau"])
        # ── Lecture automatique — le rapport se commente tout seul ─────
        lecture = []
        _lrep = _num(ft.get("la_repos"))
        if _lrep and _lrep > 2.0:
            lecture.append(
                f"Lactate de repos élevé ({_lrep:.1f} mmol/L) — à refaire "
                "à froid un matin : si ça persiste, alléger 2–3 jours.")
        if p_sv1:
            lecture.append(
                f"SV1 ≈ {p_sv1:.0f} W (équipe : allure d'endurance fondamentale "
                "— parler en marchant avec facilité).")
        else:
            lecture.append(
                "SV1 non trouvé — il faut au moins 2 paliers faciles avec "
                "lactate qui monte doucement ; complète les piqûres des "
                "paliers bas.")
        if p_sv2 and p_sv1:
            lecture.append(
                f"SV2 ≈ {p_sv2:.0f} W (OBLA 4 mmol/L, allure seuil dure) — "
                f"écart SV1→SV2 : {p_sv2 - p_sv1:.0f} W "
                f"({(p_sv2 / max(p_sv1, 1.0) - 1.0) * 100:.0f} %).")
        else:
            lecture.append(
                "SV2 non calculé (4 mmol/L non atteint) — ajoute un dernier "
                "palier plus dur ou plus long ; c'est ce palier haut qui "
                "apporte le plus d'info quand SV1 est déjà couvert.")
        if revs and la_max is not None:
            _last_p = max(r for r in revs if r)
            if la_max >= 8.0:
                lecture.append(
                    f"Dernier palier à {_last_p:.0f} W avec {la_max:.1f} "
                    "mmol/L — très au-delà du seuil : excellent repère de "
                    "P max aérobie (comparer d'un test à l'autre).")
        if n_susp:
            lecture.append(
                f"{n_susp} palier(s) coloré(s) en suspect — relis les "
                "commentaires : une piqûre douteuse est à refaire "
                "(médiane utilisée si 2–3 mesures).")
        return {"error": None, "group": grp, "date": str(ft.get("date") or ""),
                "lieu": str(ft.get("lieu") or ""),
                "fcmax": _num(ft.get("fcmax")), "poids": _num(ft.get("poids")),
                "la_repos": _num(ft.get("la_repos")),
                "n_pal": len(paliers_raw), "paliers": paliers_raw,
                "p_sv1": p_sv1, "hr_sv1": hr_sv1, "p_sv2": p_sv2,
                "hr_sv2": hr_sv2,
                "wkg_sv1": (p_sv1 / (ft.get("poids") or 70.0)) if p_sv1 else None,
                "wkg_sv2": (p_sv2 / (ft.get("poids") or 70.0)) if p_sv2 else None,
                "p_rev_max": max(revs) if revs else None,
                "wkg_max": max(wkgs) if wkgs else None,
                "la_max": max(las) if las else None,
                "lecture": lecture,
                "zones_w": zw, "zones_hr": zh, "fig": fig, "n_suspects": n_susp}

    st.subheader("🩸 Test lactate de terrain — paliers sur le même terrain")
    st.caption(
        "Protocole : échauffement, puis 3 à 6 paliers de même durée sur le même "
        "terrain, en montant d'intensité à chaque palier (ex. 4 × 4′ de facile à "
        "fort), repos ~2′ entre paliers, lactate prélevé juste après chaque palier, "
        "FCmax relevée en fin de test. La puissance de chaque palier est révisée avec "
        "la formule équipe (FC vs 80 % du FCmax), puis SV1/SV2 et les zones sont "
        "calculées sur la puissance révisée. 🔴 suspect · 🟠 vigilance.")

    fdata = st.session_state.setdefault("field_tests", {})
    _fmeth = st.radio("Méthode SV1", list(SV1_METHODS.keys()),
                      key="field_sv1_method", horizontal=True)

    _FIELD_EDIT_KEYS = ("field_date", "field_lieu", "field_fcmax",
                        "field_kg", "field_lrep", "field_paliers_ed")
    opts = list(sel) + [n for n in fdata if n not in sel]
    if not opts:
        st.info("Sélectionne d'abord les coureurs du test en haut de la page.")
        st.stop()
    try:
        field_rider = st.pills("Coureur à saisir — un clic suffit",
                               opts, key="field_rider",
                               default=[opts[0]],
                               help="Si plusieurs coureurs se pointent en "
                                    "même temps, passe de l'un à l'autre en "
                                    "un clic : les modifs non enregistrées "
                                    "d'un coureur ne touchent jamais l'autre.")
    except Exception:
        field_rider = st.selectbox("Coureur à saisir / modifier", opts,
                                   key="field_rider")
    if not field_rider:
        field_rider = opts[0]
    # Cloisonnement : si le coureur courant a changé depuis le run précédent
    # (clic, suppression, arrivée), on jette les éditions non enregistrées
    # de l'ancien coureur — elles ne peuvent plus fuir dans sa sauvegarde,
    # ni dans le rapport d'un autre.
    _prev_f = st.session_state.get("field_rider_prev")
    if _prev_f is not None and _prev_f != field_rider:
        for _k in _FIELD_EDIT_KEYS:
            st.session_state.pop(_k, None)
    st.session_state["field_rider_prev"] = field_rider

    ft = fdata.get(field_rider, {})
    _pals_prev = ft.get("paliers")
    _FIELD_COLS = ["Durée (min)", "P moy. (W)", "FC moy. (bpm)",
                   "Lactate (mmol/L)", "Lactate 2 (mmol/L)",
                   "Lactate 3 (mmol/L)", "RPE (/10)", "Commentaire"]
    if not isinstance(_pals_prev, pd.DataFrame) or _pals_prev.empty:
        _pals_prev = pd.DataFrame({
            "Durée (min)": [4.0, 4.0, 4.0, 4.0],
            "P moy. (W)": [None] * 4, "FC moy. (bpm)": [None] * 4,
            "Lactate (mmol/L)": [None] * 4, "Lactate 2 (mmol/L)": [None] * 4,
            "Lactate 3 (mmol/L)": [None] * 4, "RPE (/10)": [None] * 4,
            "Commentaire": [""] * 4})
    try:
        with st.form("field_form", clear_on_submit=False):
            hc1, hc2, hc3, hc4, hc5 = st.columns(5)
            f_date = hc1.text_input("Date", str(ft.get("date") or ""),
                                    placeholder="JJ/MM/AAAA")
            f_lieu = hc2.text_input("Lieu (montée utilisée)",
                                    str(ft.get("lieu") or ""))
            _fcmax_v = _num(ft.get("fcmax")) or 190
            f_fcmax = hc3.number_input("FCmax (bpm)", 120, 230, int(_fcmax_v))
            _kg_v = _num(ft.get("poids")) or weight_of(field_rider)
            f_kg = hc4.number_input("Poids (kg)", 30.0, 120.0, float(_kg_v), 0.5)
            _lrep_v = _num(ft.get("la_repos")) or 0.8
            f_lrep = hc5.number_input("Lactate repos (mmol/L)", 0.0, 20.0,
                                      float(_lrep_v), 0.1, format="%.1f")
            st.markdown(
                "**Paliers** — 1 ligne = 1 effort, de plus en plus fort. Ils "
                "sont triés par puissance pour l'analyse. Doute sur une "
                "piqûre ? saisis 2 ou 3 lactates (1, 2, 3) : l'analyse "
                "retient la **médiane** — une pique ratée ne fausse plus le "
                "seuil. Note le pourquoi en commentaire, il suit le palier "
                "jusqu'au rapport.")
            pals = st.data_editor(
                _pals_prev.reindex(columns=_FIELD_COLS),
                key="field_paliers_ed",
                hide_index=True, use_container_width=True, num_rows="dynamic",
                column_config={
                    "Durée (min)": st.column_config.NumberColumn(
                        "Durée (min)", min_value=0.0, max_value=120.0, step=0.5),
                    "P moy. (W)": st.column_config.NumberColumn(
                        "P moy. (W)", min_value=50, max_value=1200, step=1),
                    "FC moy. (bpm)": st.column_config.NumberColumn(
                        "FC moy. (bpm)", min_value=80, max_value=230, step=1),
                    "Lactate (mmol/L)": st.column_config.NumberColumn(
                        "Lactate 1 (mmol/L)", min_value=0.0, max_value=20.0,
                        step=0.1, format="%.1f",
                        help="Première mesure (obligatoire pour l'analyse)."),
                    "Lactate 2 (mmol/L)": st.column_config.NumberColumn(
                        "Lactate 2", min_value=0.0, max_value=20.0, step=0.1,
                        format="%.1f",
                        help="Deuxième mesure si doute — médiane utilisée."),
                    "Lactate 3 (mmol/L)": st.column_config.NumberColumn(
                        "Lactate 3", min_value=0.0, max_value=20.0, step=0.1,
                        format="%.1f",
                        help="Troisième mesure si doute — médiane utilisée."),
                    "RPE (/10)": st.column_config.NumberColumn(
                        "RPE (/10)", min_value=0.0, max_value=10.0, step=0.5,
                        format="%.1f"),
                    "Commentaire": st.column_config.TextColumn(
                        "Commentaire",
                        help="Ex. « pique 2 douteuse », « vent », « jambe "
                             "lourde » — rappelé dans le rapport et l'export."),
                })
            saved_ft = st.form_submit_button(
                f"💾 Enregistrer le test de {field_rider}")
    except Exception:
        st.warning("Tableau réinitialisé (état incompatible détecté).")
        st.rerun()
    if saved_ft:
        fdata[field_rider] = {"date": f_date, "lieu": f_lieu, "fcmax": f_fcmax,
                              "poids": f_kg, "la_repos": f_lrep,
                              "paliers": pals.copy()
                              if isinstance(pals, pd.DataFrame) else pals,
                              "group": group_of.get(field_rider, "—")}
        st.session_state["field_report"] = {}
        st.success(f"Test de {field_rider} enregistré — paliers triés par "
                   "puissance pour l'analyse ; les éditions non enregistrées "
                   "d'un autre coureur ne sont jamais reprises.")

    _entered_f = [n for n in fdata if isinstance(fdata[n].get("paliers"),
                                                 pd.DataFrame)]
    if _entered_f:
        _rem = st.multiselect("🗑️ Retirer des tests enregistrés", _entered_f,
                              key="field_remove")
        if st.button("♻️ Retirer ces tests", key="field_rm_btn") and _rem:
            for _nm in _rem:
                fdata.pop(_nm, None)
            # purge aussi le formulaire en cours : rien de leurs éditions ne
            # doit resservir (c'était la fuite du bug des lignes aberrantes)
            for _k in _FIELD_EDIT_KEYS:
                st.session_state.pop(_k, None)
            st.session_state.pop("field_rider_prev", None)
            st.session_state["field_report"] = {}
            st.rerun()

    if st.button("📊 Générer les rapports — tous les tests saisis",
                 type="primary", key="field_report_btn"):
        st.session_state["field_report"] = {}
        with st.spinner("Analyse de tous les tests…"):
            for _n in _entered_f:
                _ready = sum(
                    1 for _, _r in fdata[_n]["paliers"].iterrows()
                    if any(_num(_r.get(c)) for c in ("Lactate (mmol/L)",
                                                     "Lactate 2 (mmol/L)",
                                                     "Lactate 3 (mmol/L)")))
                if _ready < 2:
                    st.session_state["field_report"][_n] = {
                        "error": "moins de 2 lactates saisis — seuils impossibles"}
                else:
                    st.session_state["field_report"][_n] = field_report_for(
                        _n, fdata[_n], _fmeth)
        st.rerun()

    report = st.session_state.get("field_report", {})

    if not _entered_f:
        st.info("Enregistre le test d'un coureur ci-dessus (au moins 2 paliers "
                "avec lactate), puis clic sur 📊 Générer les rapports.")
        st.stop()

    # ── Rapports individuels ─────────────────────────────────────────────
    for _n in _entered_f:
        rep = report.get(_n)
        if rep is None:
            st.markdown(f"#### {_n} — ⏳ clic sur 📊 Générer les rapports")
            continue
        if isinstance(rep, dict) and rep.get("error"):
            st.markdown(f"#### {_n}")
            st.warning(f"{_n} : {rep['error']}")
            continue
        with st.expander(
                f"📋 Rapport — {_n} · {rep['group']} · {rep['date'] or 'sans date'}"
                f" · {rep['lieu'] or 'sans lieu'}", expanded=True):
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("SV1", _fmt(rep["p_sv1"], 0, " W"),
                      _fmt(rep["hr_sv1"], 0, " bpm"))
            k2.metric("SV2", _fmt(rep["p_sv2"], 0, " W"),
                      _fmt(rep["hr_sv2"], 0, " bpm"))
            k3.metric("SV1 W/kg", _fmt(rep["wkg_sv1"], 2, " W/kg"),
                      f"{rep['la_max']:.1f} mmol max" if rep["la_max"] else None)
            k4.metric("P max test", _fmt(rep["p_rev_max"], 0, " W"),
                      _fmt(rep["wkg_max"], 2, " W/kg"))
            if rep.get("lecture"):
                st.markdown("**📖 Lecture automatique**")
                for _l in rep["lecture"]:
                    st.markdown(f"- {_l}")
            dfv = pd.DataFrame(rep["paliers"])
            st.dataframe(dfv.style.apply(_palette, axis=1).hide(axis="index"),
                         use_container_width=True)
            if rep["zones_w"]:
                zl, zr = st.columns(2)
                zl.markdown("**Zones puissance (puissance révisée)**")
                zl.dataframe(pd.DataFrame(
                    [{"Zone": lbl,
                      "De (W)": "—" if b is None else round(b),
                      "À (W)": "—" if h is None else round(h)}
                     for lbl, b, h in rep["zones_w"]]),
                    hide_index=True, use_container_width=True)
                zr.markdown("**Zones fréquence cardiaque**")
                if rep["zones_hr"]:
                    zr.dataframe(pd.DataFrame(
                        [{"Zone": lbl,
                          "De (bpm)": "—" if b is None else round(b),
                          "À (bpm)": "—" if h is None else round(h)}
                         for lbl, b, h in rep["zones_hr"]]),
                        hide_index=True, use_container_width=True)
                else:
                    zr.info("FC manquantes aux paliers — zones FC indisponibles.")
            else:
                st.info("Seuils incomplets — complète les lactates ou ajuste "
                        "FCmax/poids, puis regénère les rapports.")
            if rep["fig"] is not None:
                st.plotly_chart(rep["fig"], use_container_width=True)
    st.caption("Revoir un test : sélectionne le coureur en haut, modifie les "
               "paliers, enregistre, puis regénère les rapports.")

    # ── Vue groupe ───────────────────────────────────────────────────────
    ok_reports = {n: r for n, r in report.items()
                  if isinstance(r, dict) and not r.get("error")}
    if ok_reports:
        st.subheader("Vue groupe — puissance révisée aux seuils")
        cmp_rows = [{"Coureur": n, "Groupe": rep["group"], "Paliers": rep["n_pal"],
                     "P@SV1 (W)": _round0(rep["p_sv1"]),
                     "W/kg@SV1": _round2(rep["wkg_sv1"]),
                     "P@SV2 (W)": _round0(rep["p_sv2"]),
                     "W/kg@SV2": _round2(rep["wkg_sv2"]),
                     "P max (W)": _round0(rep["p_rev_max"]),
                     "W/kg max": _round2(rep["wkg_max"]),
                     "Lact. max": _round1(rep["la_max"]),
                     "Suspects": rep["n_suspects"]}
                    for n, rep in ok_reports.items()]
        st.dataframe(pd.DataFrame(cmp_rows), hide_index=True,
                     use_container_width=True)
        zz1, zz2 = st.columns(2)
        zz1.dataframe(pd.DataFrame(
            [{"Coureur": n,
              "Z1 max": round(rep["zones_w"][0][2])
              if rep["zones_w"] and len(rep["zones_w"][0]) > 2
              and rep["zones_w"][0][2] else None,
              "Z2 → Z3": round((rep["zones_w"][1][1] + rep["zones_w"][1][2]) / 2)
              if rep["zones_w"] else None,
              "Z3 → Z4": round((rep["zones_w"][2][1] + rep["zones_w"][2][2]) / 2)
              if rep["zones_w"] else None,
              "Z4 base": round(rep["zones_w"][3][1])
              if rep["zones_w"] else None}
             for n, rep in ok_reports.items()]),
            hide_index=True, use_container_width=True)
        zz1.caption("Résumé des zones (W révisée) : haut de Z1, milieu Z2→Z3, "
                    "milieu Z3→Z4, base Z4.")
        zz2.dataframe(pd.DataFrame(
            [{"Coureur": n, "Groupe": rep["group"], "Seuil": se,
              "W (révisée)": round(pv),
              "W/kg": round(pv / (rep["poids"] or 70), 2)}
             for n, rep in ok_reports.items()
             for se, pv in (("SV1", rep["p_sv1"]), ("SV2", rep["p_sv2"]),
                            ("P max", rep["p_rev_max"])) if pv]),
            hide_index=True, use_container_width=True)
        zz2.caption("Cibles seuils par coureur.")

        grp_sel_f = st.selectbox("Groupe affiché",
                                 ["Tous les groupes"] + GROUPS,
                                 key="field_rep_grp")
        GROUP_COLORS = {"WT Hommes": "#dc2626", "WT Femmes": "#db2777",
                        "Conti Hommes": "#2563eb", "Conti Femmes": "#16a34a"}
        figg = go.Figure()
        for _n, rep in ok_reports.items():
            if grp_sel_f != "Tous les groupes" and rep["group"] != grp_sel_f:
                continue
            pts_ok = [(p["Durée (min)"], p["W/kg"]) for p in rep["paliers"]
                      if p.get("W/kg") and p["Durée (min)"]]
            if not pts_ok:
                continue
            lvl = ("suspect"
                   if any(p["_niveau"] == "rouge" for p in rep["paliers"])
                   else ("vigilance"
                         if any(p["_niveau"] for p in rep["paliers"]) else "ok"))
            figg.add_trace(go.Scatter(
                x=[p[0] for p in pts_ok], y=[p[1] for p in pts_ok],
                mode="markers+lines", name=_n,
                marker=dict(color=GROUP_COLORS.get(rep["group"], "#64748b"),
                            symbol="diamond-open" if lvl != "ok" else "circle",
                            size=10),
                line=dict(color=GROUP_COLORS.get(rep["group"], "#64748b")),
                customdata=[rep["group"]] * len(pts_ok),
                hovertemplate=f"{_n}<br>%{{x:.1f}}′ · %{{y:.2f}} W/kg"
                              "<extra>%{customdata}</extra>"))
        figg.update_layout(template="plotly_white", height=380,
                           xaxis_title="Durée du palier (min)",
                           yaxis_title="W/kg (puissance révisée)",
                           legend=dict(orientation="h", yanchor="bottom",
                                       y=1.02, xanchor="right", x=1))
        if len(figg.data):
            st.plotly_chart(figg, use_container_width=True)
            st.caption("Losange ouvert = rapport avec suspect(s).")

        lon_rows = [{"Coureur": _n, "Groupe": rep["group"], "Date": rep["date"],
                     "Lieu": rep["lieu"], "FCmax": rep["fcmax"],
                     "Poids": rep["poids"], "Lactate repos": rep["la_repos"],
                     "Palier": p["Palier"], "Durée (min)": p["Durée (min)"],
                     "P moy. (W)": p["P moy. (W)"],
                     "FC moy. (bpm)": p["FC moy. (bpm)"],
                     "Lactate (mmol/L)": p["Lactate (mmol/L)"],
                     "Lactates saisis": p.get("Lactates saisis", ""),
                     "RPE": p["RPE"],
                     "Commentaire": p.get("Commentaire", ""),
                     "P révisée (W)": _round0(p["P révisée (W)"]),
                     "W/kg (révisée)": p.get("W/kg"),
                     "⚠️ Suspect": p["⚠️ Suspect"]}
                    for _n, rep in ok_reports.items()
                    for p in rep["paliers"]]
        st.download_button("⬇️ Tests terrain — tous les paliers (CSV)",
                           pd.DataFrame(lon_rows).to_csv(index=False, sep=";",
                                                         decimal=",")
                           .encode("utf-8-sig"), "tests_lactate_terrain.csv",
                           "text/csv", key="field_csv_long")
# ═══════════════════════════════ TEST 5' & 20' — CP ═══════════════════════════════
elif tool == "cp":
    st.subheader("Test 5′ & 20′ à bloc — CP et zones d'entraînement")
    st.caption(
        "Une ligne par coureur : deux efforts maximaux, 5 min puis 20 min (récupération "
        "complète entre les deux). CP et W′ sont déduits du modèle puissance-critique à "
        "2 points (W′ = 400 × (P5′ − P20′), CP = P20′ − W′/1200) et les 6 zones "
        "d'entraînement en découlent. Sans 5′ : CP estimée par la règle 95 % × P20′. "
        "🔴 = données à vérifier.")

    CP_COLS = ["Nom", "Groupe", "P 5′ (W)", "FC moy. 5′ (bpm)", "P 20′ (W)",
               "FC moy. 20′ (bpm)", "Poids (kg)"]
    cp_key = "cp_collect"
    if not isinstance(st.session_state.get(cp_key), pd.DataFrame) or \
            st.session_state[cp_key].empty:
        st.session_state[cp_key] = pd.DataFrame(
            [{"Nom": "", "Groupe": GROUPS[0], "P 5′ (W)": None,
              "FC moy. 5′ (bpm)": None, "P 20′ (W)": None,
              "FC moy. 20′ (bpm)": None, "Poids (kg)": None}])
    st.caption("Saisis puis clique sur 💾 Enregistrer — rien n'est perdu "
               "pendant la frappe.")
    try:
        with st.form("cp_form", clear_on_submit=False):
            cdf = st.data_editor(
                st.session_state[cp_key].reindex(columns=CP_COLS), key="cp_ed",
                hide_index=True, use_container_width=True, num_rows="dynamic",
                column_config={
                    "Nom": st.column_config.TextColumn("Nom", required=True),
                    "Groupe": st.column_config.SelectboxColumn("Groupe", options=GROUPS,
                                                               required=True),
                    "P 5′ (W)": st.column_config.NumberColumn(
                        "P 5′ (W)", min_value=100, max_value=1200, step=1),
                    "FC moy. 5′ (bpm)": st.column_config.NumberColumn(
                        "FC moy. 5′ (bpm)", min_value=80, max_value=230, step=1),
                    "P 20′ (W)": st.column_config.NumberColumn(
                        "P 20′ (W)", min_value=100, max_value=1200, step=1),
                    "FC moy. 20′ (bpm)": st.column_config.NumberColumn(
                        "FC moy. 20′ (bpm)", min_value=80, max_value=230, step=1),
                    "Poids (kg)": st.column_config.NumberColumn("Poids (kg)",
                                                                min_value=30.0,
                                                                max_value=120.0,
                                                                step=0.5),
                })
            saved_c = st.form_submit_button("💾 Enregistrer le test")
    except Exception:
        st.session_state[cp_key] = pd.DataFrame([{c: None for c in CP_COLS}])
        st.warning("Tableau réinitialisé (état incompatible détecté).")
        st.rerun()
    if saved_c:
        st.session_state[cp_key] = cdf

    def cp_computed(df):
        rows = []
        raw = df.to_dict("records")
        for r in raw:
            nom = str(r.get("Nom") or "").strip()
            if not nom or nom.lower() == "nan":
                continue
            p5 = _num(r.get("P 5′ (W)"))
            p20 = _num(r.get("P 20′ (W)"))
            fc5 = _num(r.get("FC moy. 5′ (bpm)"))
            fc20 = _num(r.get("FC moy. 20′ (bpm)"))
            kg = _num(r.get("Poids (kg)"))
            grp = str(r.get("Groupe") or "—")
            m = cp_from_5_20(p5, p20) if (p5 and p20) else None
            flags = []
            if p5 and p20 and not m:
                flags.append("5′ ≤ 20′ — à vérifier")
            if m:
                cp, wp, est = m["cp"], m["wprime"], False
                if not (8000 <= wp <= 40000):
                    flags.append("W′ inhabituel")
            elif p20:
                cp, wp, est = 0.95 * p20, None, True
                flags.append("CP estimée 95 % (5′ manquant)")
            else:
                cp = wp = None
                est = False
            if cp and not (200 <= cp <= 650):
                flags.append("CP inhabituelle")
            cp_wkg = (cp / kg) if (cp and kg) else None
            if cp and not kg:
                flags.append("poids manquant")
            grp_vals = []
            for x in raw:
                if str(x.get("Groupe") or "—") != grp or \
                        not str(x.get("Nom") or "").strip():
                    continue
                xm = cp_from_5_20(_num(x.get("P 5′ (W)")), _num(x.get("P 20′ (W)"))) \
                    if (_num(x.get("P 5′ (W)")) and _num(x.get("P 20′ (W)"))) else None
                xcp = xm["cp"] if xm else (
                    0.95 * _num(x.get("P 20′ (W)")) if _num(x.get("P 20′ (W)")) else None)
                xkg = _num(x.get("Poids (kg)"))
                if xcp and xkg:
                    grp_vals.append(xcp / xkg)
            if cp_wkg and len(grp_vals) >= 3:
                arr = np.array(grp_vals, dtype=float)
                sd = float(arr.std())
                if sd > 0 and abs(cp_wkg - float(arr.mean())) > 2 * sd:
                    flags.append("CP W/kg atypique vs groupe")
            rows.append({
                "Nom": nom, "Groupe": grp,
                "P 5′ (W)": p5, "FC moy. 5′ (bpm)": fc5,
                "P 20′ (W)": p20, "FC moy. 20′ (bpm)": fc20,
                "Poids (kg)": kg,
                "CP (W)": round(cp) if cp else None,
                "W′ (kJ)": round(wp / 1000.0, 1) if wp else None,
                "CP (W/kg)": round(cp_wkg, 2) if cp_wkg else None,
                "FC seuil ≈ FC 20′": fc20,
                "⚠️ Suspect": " · ".join(flags),
                "_niveau": "rouge" if ("à vérifier" in " · ".join(flags) or
                                       "inhabituelle" in " · ".join(flags) or
                                       "atypique" in " · ".join(flags))
                else ("orange" if flags else "")})
        return pd.DataFrame(rows)

    cdisp = cp_computed(cdf)
    if cdisp.empty:
        st.info("Ajoute une ligne (au moins un Nom + P 20′) — CP, W′, W/kg et zones se "
                "calculent automatiquement.")
    else:
        def _paint_cp(row):
            color = ("#fee2e2" if row["_niveau"] == "rouge"
                     else "#fef3c7" if row["_niveau"] else "")
            return [f"background-color:{color}"] * len(row) if color else [""] * len(row)

        st.dataframe(cdisp.style.apply(_paint_cp, axis=1).hide(axis="index"),
                     use_container_width=True)

        st.subheader("Zones d'entraînement par coureur")

        def _pct_df(default_pcts, key):
            if not isinstance(st.session_state.get(key), pd.DataFrame):
                st.session_state[key] = pd.DataFrame(
                    [{"Zone": lbl,
                      "Bas (%)": None if lo is None else round(lo * 100),
                      "Haut (%)": None if hi is None else round(hi * 100)}
                     for lbl, lo, hi in default_pcts])
            return st.session_state[key]

        with st.expander("🎛️ Régler les % des zones — selon le niveau ou le profil"):
            st.caption("Puissance : % de CP. FC : % de la FC au seuil (≈ FC moy. 20′). "
                       "Vide ou 0 = borne ouverte. Appliqué à tous les coureurs — "
                       "les % affichés sous « Zones » suivent toujours ces réglages.")
            _edw = st.data_editor(_pct_df(CP_ZONE_PCTS, "cp_pct_base_w"),
                                  key="cp_zone_pct_ed_w", hide_index=True,
                                  use_container_width=True, num_rows="fixed",
                                  column_config={
                                      "Zone": st.column_config.TextColumn(
                                          "Zone", disabled=True),
                                      "Bas (%)": st.column_config.NumberColumn(
                                          min_value=0, max_value=300, step=1),
                                      "Haut (%)": st.column_config.NumberColumn(
                                          min_value=0, max_value=300, step=1)})
            _edf = st.data_editor(_pct_df(FC_ZONE_PCTS, "cp_pct_base_f"),
                                  key="cp_zone_pct_ed_f", hide_index=True,
                                  use_container_width=True, num_rows="fixed",
                                  column_config={
                                      "Zone": st.column_config.TextColumn(
                                          "Zone", disabled=True),
                                      "Bas (%)": st.column_config.NumberColumn(
                                          min_value=0, max_value=300, step=1),
                                      "Haut (%)": st.column_config.NumberColumn(
                                          min_value=0, max_value=300, step=1)})
            _zr1, _zr2 = st.columns(2)
            if _zr1.button("↩️ Watts : % standards", key="cp_pct_reset_w"):
                for _k in ("cp_zone_pct_ed_w", "cp_pct_base_w"):
                    st.session_state.pop(_k, None)
                st.rerun()
            if _zr2.button("↩️ FC : % standards", key="cp_pct_reset_f"):
                for _k in ("cp_zone_pct_ed_f", "cp_pct_base_f"):
                    st.session_state.pop(_k, None)
                st.rerun()

        def _to_pcts(edf, fallback):
            if not isinstance(edf, pd.DataFrame) or edf.empty:
                return list(fallback)
            out = []
            for i, (lbl, _lo, _hi) in enumerate(fallback):
                if i < len(edf):
                    _r = edf.iloc[i]
                    lo = _num(_r.get("Bas (%)"))
                    hi = _num(_r.get("Haut (%)"))
                    out.append((lbl,
                                lo / 100.0 if lo else None,
                                hi / 100.0 if hi else None))
                else:
                    out.append(fallback[i])
            return out

        _pct_w = _to_pcts(st.session_state.get("cp_zone_pct_ed_w"), CP_ZONE_PCTS)
        _pct_f = _to_pcts(st.session_state.get("cp_zone_pct_ed_f"), FC_ZONE_PCTS)
        for _, row in cdisp.iterrows():
            cp = _num(row["CP (W)"])
            if not cp:
                continue
            fc20 = _num(row["FC seuil ≈ FC 20′"])
            with st.expander(f"{row['Nom']} — {row['Groupe']} · CP {cp:.0f} W"
                             + (f" ({row['CP (W/kg)']:.2f} W/kg)"
                                if row["CP (W/kg)"] else "")):
                zw = cp_zone_table(cp, _pct_w)
                zf = fc_zone_table(fc20, _pct_f) if fc20 else None
                cza, czb = st.columns(2)
                with cza:
                    st.markdown("**Zones puissance**")
                    if zw:
                        st.dataframe(pd.DataFrame(
                            [{"Zone": lbl,
                              "% bas": "ouvert" if lp is None else f"{lp * 100:.0f} %",
                              "% haut": "ouvert" if hp is None else f"{hp * 100:.0f} %",
                              "Bas (W)": round(lo) if lo else "—",
                              "Haut (W)": round(hi) if hi else "ouvert"}
                             for (lbl, lp, hp), (_, lo, hi) in zip(_pct_w, zw)]),
                            hide_index=True, use_container_width=True)
                    else:
                        st.info("CP indisponible.")
                with czb:
                    st.markdown("**Zones fréquence cardiaque**")
                    if zf:
                        st.dataframe(pd.DataFrame(
                            [{"Zone": lbl,
                              "% bas": "ouvert" if lp is None else f"{lp * 100:.0f} %",
                              "% haut": "ouvert" if hp is None else f"{hp * 100:.0f} %",
                              "Bas (bpm)": round(lo) if lo else "—",
                              "Haut (bpm)": round(hi) if hi else "ouvert"}
                             for (lbl, lp, hp), (_, lo, hi) in zip(_pct_f, zf)]),
                            hide_index=True, use_container_width=True)
                    else:
                        st.info("FC 20′ manquante — zones FC indisponibles.")
                st.caption("Watts : % de CP · FC : % de la FC au seuil (≈ FC moy. 20′) "
                           "— % réglables dans l'onglet 🎛️ ci-dessus.")

        st.subheader("CP (W/kg) par coureur")
        bar_df = cdisp[cdisp["CP (W/kg)"].notna()].sort_values("CP (W/kg)")
        if not bar_df.empty:
            GROUP_COLORS = {"WT Hommes": "#dc2626", "WT Femmes": "#db2777",
                            "Conti Hommes": "#2563eb", "Conti Femmes": "#16a34a"}
            fig = go.Figure()
            for g in sorted(bar_df["Groupe"].unique()):
                sub = bar_df[bar_df["Groupe"] == g]
                fig.add_trace(go.Bar(
                    y=sub["Nom"], x=sub["CP (W/kg)"], name=g, orientation="h",
                    marker_color=GROUP_COLORS.get(g, "#64748b"),
                    text=[f"{v:.2f}" for v in sub["CP (W/kg)"]],
                    textposition="outside", cliponaxis=False,
                    customdata=sub[["P 5′ (W)", "P 20′ (W)", "CP (W)",
                                    "Poids (kg)"]].fillna("—"),
                    hovertemplate="%{y} — %{customdata[2]} W (%{customdata[3]} kg)"
                                  "<br>5′ %{customdata[0]} W · 20′ "
                                  "%{customdata[1]} W<extra></extra>"))
            _mean = float(bar_df["CP (W/kg)"].mean())
            fig.add_vline(x=_mean, line_dash="dot", line_color="#64748b",
                          annotation_text=f"Équipe : {_mean:.2f} W/kg")
            fig.update_layout(template="plotly_white",
                              height=max(320, 46 * len(bar_df) + 120),
                              xaxis_title="CP (W/kg)",
                              legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                          xanchor="right", x=1))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Renseigne la puissance 20′ (et le poids) pour afficher le graphe.")

        st.download_button("⬇️ Test 5′ & 20′ (CSV)",
                           cdisp.drop(columns=["_niveau"]).to_csv(
                               index=False, sep=";", decimal=",").encode("utf-8-sig"),
                           "test_5_20_cp.csv", "text/csv")


# ═════════════════════════ TEST 3′ ALL-OUT — CP & W′ ═════════════════════════
elif tool == "cp3":

    def _cp_of(n):
        """CP d'un coureur : 1) outil 5'/20' en session ; 2) profil de la fiche."""
        cdf = st.session_state.get("cp_collect")
        if isinstance(cdf, pd.DataFrame) and not cdf.empty and "Nom" in cdf.columns:
            _m = cdf[cdf["Nom"].astype(str).str.strip() == str(n)]
            if not _m.empty:
                _c = _num(_m.iloc[0].get("CP (W)"))
                if _c:
                    return _c, "test 5′/20′"
        nr = rider_by_name.get(n, {})
        _c = _num(nr.get("cp")) or _num(nr.get("p60m"))
        return (_c, "profil fiche") if _c else (None, "—")

    with st.expander("❓ Le test 3′ all-out — en 40 secondes"):
        st.markdown(
            "**Le principe.** Un seul effort : 3 minutes à bloc, départ quasi "
            "arrêté (plat ou petite côte, braquet chose). Dans les dernières "
            "secondes, le coureur tourne sur son moteur d'endurance : la puissance "
            "des **30 dernières secondes est sa CP** (puissance critique). Le "
            "travail fourni AU-DESSUS de cette CP pendant les 3 min donne la "
            "**W′** (le réservoir au-delà du seuil, en kJ).")
        st.markdown(
            "**À quoi ça sert.** Contrôler le 5′/20′ sans refaire deux efforts "
            "durs : si la CP 3′ diverge de la CP 5′/20′, un des deux tests "
            "n'était pas à bloc (ou le coureur était fatigué, ou le 20′ était "
            "fractionné/venté). Une CP validée alimente la fiche, les prévisions "
            "et toutes les zones.")
        st.markdown(
            "**Un bon test d'abord.** Départ FRANC : la tranche 0–30 s doit "
            "dépasser ~115 % de la CP finale (sinon W′ sous-estimée). Plateau : "
            "les tranches 150–180 s et 120–150 s quasi identiques (±5 %). "
            "Sur le terrain : coureur motivé, compteur avec puissance, note les "
            "6 tranches de 30 s juste après l'arrêt du coureur.")

    try:
        cp3_rider = st.pills("Coureur — un clic", sel, key="cp3_rider",
                             default=[sel[0]],
                             help="Un coureur à la fois — les données déjà "
                                  "enregistrées sont reprises.")
    except Exception:
        cp3_rider = st.selectbox("Coureur", sel, key="cp3_rider")
    if not cp3_rider:
        cp3_rider = sel[0]
    _CP3_EDIT_KEYS = ("cp3_rider", "cp3_s1", "cp3_s2", "cp3_s3",
                      "cp3_s4", "cp3_s5", "cp3_s6", "cp3_fc") + ("cp3_cmt",)
    _prev3 = st.session_state.get("cp3_data", {}).get(cp3_rider, {})
    _pv3_segs = _prev3.get("segs") or [0, 0, 0, 0, 0, 0]
    _pv3_fc = _prev3.get("fc")
    _pv3_cmt = _prev3.get("cmt") or ""
    with st.form("cp3_entry"):
        st.markdown(f"**{cp3_rider}** — puissance MOYENNE de chaque tranche de 30 s")
        _sc = st.columns(3)
        s1_ = _sc[0].number_input("0–30 s (W)", 0, 1600, int(_pv3_segs[0] or 0),
                                  key="cp3_s1", help="Départ à bloc — doit être le pic de l'effort.")
        s2_ = _sc[1].number_input("30–60 s (W)", 0, 1600, int(_pv3_segs[1] or 0), key="cp3_s2")
        s3_ = _sc[2].number_input("60–90 s (W)", 0, 1600, int(_pv3_segs[2] or 0), key="cp3_s3")
        s4_ = _sc[0].number_input("90–120 s (W)", 0, 1600, int(_pv3_segs[3] or 0), key="cp3_s4")
        s5_ = _sc[1].number_input("120–150 s (W)", 0, 1600, int(_pv3_segs[4] or 0), key="cp3_s5")
        s6_ = _sc[2].number_input("150–180 s (W)", 0, 1600, int(_pv3_segs[5] or 0), key="cp3_s6",
                                  help="La CLÉ du test : sa moyenne des 30 dernières secondes = CP.")
        fcf = st.number_input("FC dernière minute (bpm)", 80, 230,
                              int(_pv3_fc or 150), key="cp3_fc")
        cmt3 = st.text_input("Commentaire", _pv3_cmt,
                             placeholder="ex. vent de face, départ à 25 km/h, spé n'a pas tout donné",
                             key="cp3_cmt")
        if st.form_submit_button("💾 Enregistrer ce test"):
            st.session_state.setdefault("cp3_data", {})[cp3_rider] = {
                "segs": [s1_ or None, s2_ or None, s3_ or None,
                         s4_ or None, s5_ or None, s6_ or None],
                "fc": fcf or None, "cmt": cmt3 or "",
                "group": group_of.get(cp3_rider, "—"),
                "weight": weight_of(cp3_rider)}
            st.success(f"✅ Test 3′ enregistré pour {cp3_rider}.")

    data3 = st.session_state.get("cp3_data", {})
    entered3 = [n for n in sel if n in data3]
    rows3 = []
    for n in entered3:
        d3 = data3[n]
        segs = [_num(x) for x in (d3.get("segs") or [None] * 6)]
        if any(x is None for x in segs):
            continue
        cp3 = segs[5]
        if not cp3:
            continue
        plateau = abs(segs[5] - segs[4]) / max(segs[5], 1.0)
        allout = segs[0] >= 1.15 * cp3
        wprime = sum(max(0.0, x - cp3) for x in segs) * 30.0 / 1000.0
        cp_ref, src_ref = _cp_of(n)
        rows3.append({"Coureur": n, "Groupe": d3.get("group", "—"),
                      "CP 3′ (W)": round(cp3),
                      "CP 3′ (W/kg)": round(cp3 / weight_of(n), 2),
                      "W′ (kJ)": round(wprime, 1),
                      "Départ dur": "✅" if allout else "⚠️",
                      "Plateau": "✅" if plateau < 0.05 else f"⚠️ ±{plateau * 100:.0f} %",
                      "CP 5′/20′": round(cp_ref) if cp_ref else "",
                      "Source CP": src_ref,
                      "Écart": f"{(cp3 - cp_ref) / cp_ref * 100:+.1f} %" if cp_ref else "",
                      "FC fin": d3.get("fc") or "—",
                      "Commentaire": d3.get("cmt") or ""})
    if not rows3:
        st.info("Enregistre un test 3′ ci-dessus (les 6 tranches) pour lancer l'analyse.")
    else:
        st.subheader("Analyse")
        st.dataframe(pd.DataFrame(rows3), hide_index=True, use_container_width=True)
        for r3 in rows3:
            n = r3["Coureur"]
            d3 = data3[n]
            segs = [_num(x) for x in d3["segs"]]
            cp3 = float(segs[5])
            plateau = abs(segs[5] - segs[4]) / max(segs[5], 1.0)
            wprime = sum(max(0.0, x - cp3) for x in segs) * 30.0 / 1000.0
            cp_ref, src_ref = _cp_of(n)
            with st.expander(f"⚡ {n} — {d3.get('group', '—')} · CP 3′ {cp3:.0f} W "
                             f"({cp3 / weight_of(n):.1f} W/kg) · W′ {wprime:.1f} kJ"):
                st.markdown(
                    f"**CP 3′ : {cp3:.0f} W** · **W′ : {wprime:.1f} kJ** · "
                    f"P 0–30 s : {segs[0]:.0f} W · FC fin : {d3.get('fc') or '—'} bpm "
                    f"· {d3.get('fc') and ''}poids {weight_of(n):.1f} kg")
                if d3.get("cmt"):
                    st.caption(f"💬 {d3['cmt']}")
                fig3 = go.Figure(go.Bar(
                    x=[f"{i * 30}–{(i + 1) * 30} s" for i in range(6)],
                    y=segs, marker_color="#2563eb",
                    hovertemplate="%{x} : %{y} W<extra></extra>"))
                fig3.add_hline(y=cp3, line_dash="dot", line_color="#dc2626",
                               annotation_text=f"CP 3′ = {cp3:.0f} W")
                fig3.update_layout(template="plotly_white", height=300,
                                   yaxis_title="Puissance (W)",
                                   xaxis_title="Tranches de l'effort")
                st.plotly_chart(fig3, use_container_width=True)
                st.dataframe(pd.DataFrame(
                    [{"Tranche": f"{i * 30}–{(i + 1) * 30} s",
                      "P moy. (W)": x,
                      "% CP 3′": f"{x / cp3 * 100:.0f} %",
                      "Au-dessus de CP (W)": round(max(0.0, x - cp3))}
                     for i, x in enumerate(segs)]),
                    hide_index=True, use_container_width=True)
                st.markdown("**Validité & lecture automatique**")
                if segs[0] >= 1.15 * cp3:
                    st.caption("✅ Départ franc — la W′ est exploitable.")
                else:
                    st.caption("⚠️ Départ trop mou (0–30 s < 115 % de la CP) — "
                               "W′ sous-estimée : refais le test en mettant la "
                               "gomme dès la première seconde.")
                if plateau >= 0.05:
                    st.caption(f"⚠️ Pas de plateau net ({segs[4]:.0f} → {segs[5]:.0f} "
                               f"W, écart {plateau * 100:.0f} %) — CP peu fiable : "
                               "le coureur a encore ralenti à la fin ou le test "
                               "était trop long pour lui.")
                else:
                    st.caption("✅ Plateau net en fin d'effort — CP solide.")
                if cp_ref:
                    ecart = (cp3 - cp_ref) / cp_ref * 100
                    if abs(ecart) <= 4:
                        st.caption(f"✅ Cohérente avec la CP du test 5′/20′ "
                                   f"({src_ref} : {cp_ref:.0f} W, écart "
                                   f"{ecart:+.1f} %) — CP validée.")
                    elif ecart > 4:
                        st.caption(f"⚠️ CP 3′ au-dessus du 5′/20′ ({cp_ref:.0f} W) "
                                   f"de {ecart:+.1f} % — le 5′/20′ était "
                                   "sous-maximal (ou fractionné/venté) ; la CP 3′ "
                                   "semble plus proche de la réalité.")
                    else:
                        st.caption(f"⚠️ CP 3′ en dessous du 5′/20′ ({cp_ref:.0f} W) "
                                   f"de {ecart:.1f} % — fatigue ce jour-là ou "
                                   "effort 3′ pas à bloc ; ne baisse pas la CP "
                                   "de la fiche sur cette seule lecture.")
                zt3 = cp_zone_table(cp3)
                if zt3:
                    st.markdown("**Zones d'entraînement (en % de la CP 3′)**")
                    st.dataframe(pd.DataFrame(
                        [{"Zone": z, "Bas (W)": round(lo) if lo else "—",
                          "Haut (W)": round(hi) if hi else "ouvert"}
                         for z, lo, hi in zt3]),
                        hide_index=True, use_container_width=True)
        st.download_button("⬇️ Tests 3′ (CSV)",
                           pd.DataFrame(rows3).to_csv(index=False, sep=";",
                                                       decimal=",").encode("utf-8-sig"),
                           "tests_3min_allout.csv", "text/csv")


# ═══════════════════════════════ SPRINTS & RSA ═══════════════════════════════
elif tool == "sprint":

    def _cp_of(n):
        """CP d'un coureur : 1) outil 5'/20' en session ; 2) profil de la fiche."""
        cdf = st.session_state.get("cp_collect")
        if isinstance(cdf, pd.DataFrame) and not cdf.empty and "Nom" in cdf.columns:
            _m = cdf[cdf["Nom"].astype(str).str.strip() == str(n)]
            if not _m.empty:
                _c = _num(_m.iloc[0].get("CP (W)"))
                if _c:
                    return _c, "test 5′/20′"
        nr = rider_by_name.get(n, {})
        _c = _num(nr.get("cp")) or _num(nr.get("p60m"))
        return (_c, "profil fiche") if _c else (None, "—")

    mode_s = st.radio("Format", ["🌀 Sprints max (3–5 × ~10 s)",
                                 "🔁 RSA — sprints répétés (10 × 6 s / 30 s)"],
                      horizontal=True, key="sprint_format")

    with st.expander("❓ Explications — comment ça marche"):
        if mode_s.startswith("🌀"):
            st.markdown(
                "**Le test.** 3 à 5 sprints de ~10 s à bloc, départ LANCÉ à "
                "30–40 km/h (abri, moto ou descente douce), récup COMPLÈTE de "
                "5–8 min entre chaque. Note pour chaque sprint : le pic 1″ "
                "(pure puissance), le pic 5″ (le standard sprint), la puissance "
                "moyenne, la cadence au pic, la FC de fin, la RPE — et le lactate "
                "après le 2ᵉ et le dernier sprint (on peut monter à 6–10 mmol/L, "
                "c'est normal : le sprint sollicite fort la filière glycolytique).")
            st.markdown(
                "**Comment je le lis.** Le pic 5″ du MEILLEUR sprint comparé à la "
                "CP du coureur dit son profil : au-dessus de 2,5 × CP = gros "
                "sprinteur ; 2,0–2,5 × = offensif, gagne ses duels de côte-à-côte "
                "mais pas les arrivals massives ; sous 2,0 × = rouleur. La CHUTE "
                "du meilleur sprint au dernier = le niveau de fatigue : en course, "
                "c'est ce qui reste dans la dernière relance.")
        else:
            st.markdown(
                "**Le test.** 10 × 6 s à bloc, récup de 30 s assis (passive). "
                "Note le pic et la moyenne de chaque sprint. On calcule le "
                "S_dec (décrément de sprint) = combien de % de puissance le "
                "coureur perd en moyenne par rapport à son meilleur sprint.")
            st.markdown(
                "**Comment je le lis.** S_dec < 5 % = finisseur : il répète "
                "presque au même niveau (opperatif pour les arrivées qui "
                "s'élancent déjà fatiguées) ; 5–10 % = correct ; au-dessus de "
                "10 % = il s'essouffle vite : à travaille en RSA dans le plan. "
                "C'est souvent CE test qui départage deux coureurs à CP égale "
                "en course par étapes.")

    try:
        spr_rider = st.pills("Coureur — un clic", sel, key="spr_rider",
                             default=[sel[0]],
                             help="Un coureur à la fois — chaque coureur a son "
                                  "propre tableau, rien ne fuit d'un autre.")
    except Exception:
        spr_rider = st.selectbox("Coureur", sel, key="spr_rider")
    if not spr_rider:
        spr_rider = sel[0]

    if mode_s.startswith("🌀"):
        SPR_C = ["P pic 1″ (W)", "P pic 5″ (W)", "P moy sprint (W)",
                 "Cadence pic (rpm)", "FC fin (bpm)", "Lactate (mmol/L)",
                 "RPE (/10)", "Commentaire"]
        sk = f"spr_data_{spr_rider}"
        if sk not in st.session_state:
            st.session_state[sk] = pd.DataFrame(
                [{"Sprint": f"S{i}",
                  **{c: ("" if c == "Commentaire" else None) for c in SPR_C}}
                 for i in range(1, 6)])
        with st.form(f"spr_form_{abs(hash(spr_rider)) % 10 ** 8}",
                     clear_on_submit=False):
            sdf = st.data_editor(
                st.session_state[sk].reindex(columns=["Sprint"] + SPR_C),
                key=f"spr_ed_{spr_rider}", hide_index=True,
                use_container_width=True, num_rows="fixed",
                column_config={
                    "Sprint": st.column_config.TextColumn("Sprint", disabled=True),
                    "P pic 1″ (W)": st.column_config.NumberColumn(
                        "P pic 1″ (W)", min_value=0, max_value=2500, step=5,
                        help="Le pic instantané le plus haut vu sur la 10 s."),
                    "P pic 5″ (W)": st.column_config.NumberColumn(
                        "P pic 5″ (W)", min_value=0, max_value=2500, step=5,
                        help="Puissance moyenne des 5 MEILLEURES secondes — "
                             "le standard du sprint."),
                    "P moy sprint (W)": st.column_config.NumberColumn(
                        "P moy sprint (W)", min_value=0, max_value=2500, step=5),
                    "Cadence pic (rpm)": st.column_config.NumberColumn(
                        "Cadence pic (rpm)", min_value=0, max_value=200, step=1),
                    "FC fin (bpm)": st.column_config.NumberColumn(
                        "FC fin (bpm)", min_value=80, max_value=230, step=1),
                    "Lactate (mmol/L)": st.column_config.NumberColumn(
                        "Lactate (mmol/L)", min_value=0.0, max_value=20.0,
                        step=0.1, format="%.1f",
                        help="Après le 2ᵉ sprint et le dernier : 5–10 = normal."),
                    "RPE (/10)": st.column_config.NumberColumn(
                        "RPE (/10)", min_value=0.0, max_value=10.0, step=0.5),
                    "Commentaire": st.column_config.TextColumn("Commentaire")})
            if st.form_submit_button("💾 Enregistrer les sprints"):
                st.session_state[sk] = sdf.copy()
                st.success(f"✅ Sprints enregistrés pour {spr_rider}.")

        def _spr_rows(n):
            df = st.session_state.get(f"spr_data_{n}")
            if df is None or df.empty:
                return []
            out_ = []
            for _, r in df.iterrows():
                vals_ = {k: _num(r.get(k)) for k in
                         ("P pic 1″ (W)", "P pic 5″ (W)", "P moy sprint (W)",
                          "Cadence pic (rpm)", "FC fin (bpm)",
                          "Lactate (mmol/L)", "RPE (/10)")}
                if any(v is not None for v in vals_.values()):
                    out_.append({"Sprint": str(r.get("Sprint") or f"S{len(out_) + 1}"),
                                 **vals_,
                                 "Commentaire": str(r.get("Commentaire") or "")})
            return out_

        st.subheader("Rapport détaillé — sprints max")
        rows4 = []
        for n in sel:
            rw = _spr_rows(n)
            if len(rw) < 2:
                continue
            b1 = max((r_.get("P pic 1″ (W)") or 0) for r_ in rw)
            b5 = max((r_.get("P pic 5″ (W)") or 0) for r_ in rw)
            first, last = rw[0], rw[-1]
            p5f, p5l = (first.get("P pic 5″ (W)") or 0), (last.get("P pic 5″ (W)") or 0)
            chute = ((p5f - p5l) / p5f * 100) if p5f else 0.0
            cp_r, src_r = _cp_of(n)
            ratio = (b5 / cp_r) if (cp_r and b5) else None
            rows4.append({"Coureur": n, "Groupe": group_of.get(n, "—"),
                          "Sprints": len(rw),
                          "Pic 1″ max (W)": round(b1) if b1 else "",
                          "Pic 5″ max (W)": round(b5) if b5 else "",
                          "Pic 5″ (W/kg)": round(b5 / weight_of(n), 2) if b5 else "",
                          "R ratio P5″/CP": f"{ratio:.1f}" if ratio else "",
                          "Source CP": src_r if cp_r else "",
                          "Chute S1→Sn": f"{chute:.0f} %",
                          "Lactate max": max((r_.get("Lactate (mmol/L)") or 0)
                                             for r_ in rw) or "",
                          "Verdict": (("🌀 Profil sprinteur" if ratio >= 2.5
                                       else "⚡ Offensif" if ratio >= 2.0
                                       else "🧗 Profil rouleur")
                                      if ratio else "⏳ Il manque la CP")})
        if rows4:
            st.dataframe(pd.DataFrame(rows4), hide_index=True, use_container_width=True)
        for n in sel:
            rw = _spr_rows(n)
            if not rw:
                continue
            cp_r, src_r = _cp_of(n)
            b5 = max((r_.get("P pic 5″ (W)") or 0) for r_ in rw)
            with st.expander(f"🌀 {n} — {len(rw)} sprint(s)" +
                             (f" · meilleur pic 5″ {b5:.0f} W" if b5 else "")):
                st.dataframe(pd.DataFrame(rw), hide_index=True, use_container_width=True)
                figs = go.Figure()
                xs = [r_["Sprint"] for r_ in rw]
                for col_, colcol_, nm_ in [("P pic 1″ (W)", "#dc2626", "Pic 1″"),
                                           ("P pic 5″ (W)", "#dc2626", "Pic 5″"),
                                           ("P moy sprint (W)", "#2563eb", "P moy.")]:
                    ys = [r_.get(col_) for r_ in rw]
                    if any(v is not None for v in ys):
                        figs.add_trace(go.Bar(x=xs, y=ys, name=nm_,
                                              marker_color=colcol_))
                if cp_r:
                    figs.add_hline(y=cp_r, line_dash="dot", line_color="#64748b",
                                   annotation_text=f"CP ({src_r}) = {cp_r:.0f} W")
                figs.update_layout(template="plotly_white", barmode="group",
                                   height=320, yaxis_title="Puissance (W)",
                                   legend=dict(orientation="h", yanchor="bottom",
                                               y=1.02, xanchor="right", x=1))
                st.plotly_chart(figs, use_container_width=True)
                for r_ in rw:
                    if r_.get("Commentaire"):
                        st.caption(f"💬 {r_['Sprint']} — {r_['Commentaire']}")
                lact = [r_.get("Lactate (mmol/L)") for r_ in rw if r_.get("Lactate (mmol/L)")]
                if lact:
                    la_m = max(lact)
                    ver_ = ("🟢 Peu de sollicitation glycolytique (au-dessous de ce "
                            "qu'on attend d'un sprint vrai)" if la_m < 5
                            else "✅ Sollicitation attendue pour un sprint à bloc" if la_m <= 12
                            else "🔴 Très engorgé — vérifie les récupérations et la "
                                 "durée des sprints")
                    st.caption(f"🩸 Lactate sprint max : {la_m:.1f} mmol/L — {ver_}")
                if cp_r and b5:
                    ratio = b5 / cp_r
                    if ratio >= 2.5:
                        st.caption("🌀 Au-dessus de 2,5 × CP : gros profil "
                                   "sprinteur — garde ce coureur pour les "
                                   "arrivées massives.")
                    elif ratio >= 2.0:
                        st.caption("⚡ Entre 2,0 × et 2,5 × CP : offensif — il gagne "
                                   "les duels et les arrivées réduites, pas les "
                                   "massives.")
                    else:
                        st.caption("🧗 Sous 2,0 × CP : profil rouleur — son "
                                   "argent est ailleurs (GC, TTT, echappées).")
        st.download_button("⬇️ Sprints max (CSV)",
                           pd.DataFrame(rows4).to_csv(index=False, sep=";",
                                                       decimal=",").encode("utf-8-sig"),
                           "tests_sprints_max.csv", "text/csv")

    else:  # RSA
        RSA_C = ["P pic (W)", "P moy 6″ (W)", "RPE (/10)", "Commentaire"]
        sk = f"rsa_data_{spr_rider}"
        if sk not in st.session_state:
            st.session_state[sk] = pd.DataFrame(
                [{"Sprint": f"S{i}",
                  **{c: ("" if c == "Commentaire" else None) for c in RSA_C}}
                 for i in range(1, 11)])
        with st.form(f"rsa_form_{abs(hash(spr_rider)) % 10 ** 8}",
                     clear_on_submit=False):
            rdf = st.data_editor(
                st.session_state[sk].reindex(columns=["Sprint"] + RSA_C),
                key=f"rsa_ed_{spr_rider}", hide_index=True,
                use_container_width=True, num_rows="fixed",
                column_config={
                    "Sprint": st.column_config.TextColumn("Sprint", disabled=True),
                    "P pic (W)": st.column_config.NumberColumn(
                        "P pic (W)", min_value=0, max_value=2500, step=5),
                    "P moy 6″ (W)": st.column_config.NumberColumn(
                        "P moy 6″ (W)", min_value=0, max_value=2500, step=5,
                        help="Moyenne des 6 s du sprint (S_dec classique)."),
                    "RPE (/10)": st.column_config.NumberColumn(
                        "RPE (/10)", min_value=0.0, max_value=10.0, step=0.5),
                    "Commentaire": st.column_config.TextColumn("Commentaire")})
            if st.form_submit_button("💾 Enregistrer le RSA"):
                st.session_state[sk] = rdf.copy()
                st.success(f"✅ RSA enregistré pour {spr_rider}.")

        def _rsa_rows(n):
            df = st.session_state.get(f"rsa_data_{n}")
            if df is None or df.empty:
                return []
            out_ = []
            for _, r in df.iterrows():
                vals_ = {k: _num(r.get(k)) for k in ("P pic (W)", "P moy 6″ (W)",
                                                     "RPE (/10)")}
                if any(v is not None for v in vals_.values()):
                    out_.append({"Sprint": str(r.get("Sprint")), **vals_,
                                 "Commentaire": str(r.get("Commentaire") or "")})
            return out_

        st.subheader("Rapport détaillé — RSA")
        rows5 = []
        for n in sel:
            rw = _rsa_rows(n)
            if len(rw) < 3:
                continue
            ms_ = [r_.get("P moy 6″ (W)") or 0 for r_ in rw]
            ps_ = [r_.get("P pic (W)") or 0 for r_ in rw]
            best = max(ms_) if any(ms_) else 0
            meanok = [v for v in ms_ if v]
            sdec = (100 * (1 - (sum(meanok) / len(meanok)) / best)) if (best and meanok) else None
            rows5.append({"Coureur": n, "Groupe": group_of.get(n, "—"),
                          "Répétitions": len(rw),
                          "Meilleur 6″ (W)": round(best) if best else "",
                          "Pic max (W)": round(max(ps_)) if any(ps_) else "",
                          "S_dec": f"{sdec:.0f} %" if sdec is not None else "",
                          "S1→Sn": f"{((ms_[0] - ms_[-1]) / ms_[0] * 100):.0f} %"
                          if (ms_[0] and ms_[-1]) else "",
                          "Verdict": (("🔁 Finisseur — répète au top" if sdec < 5
                                       else "🟡 Correct" if sdec <= 10
                                       else "🔴 S'essouffle vite")
                                      if sdec is not None else "⏳ Il manque des sprints")})
        if rows5:
            st.dataframe(pd.DataFrame(rows5), hide_index=True, use_container_width=True)
        for n in sel:
            rw = _rsa_rows(n)
            if len(rw) < 2:
                continue
            ms_ = [r_.get("P moy 6″ (W)") or 0 for r_ in rw]
            ps_ = [r_.get("P pic (W)") or 0 for r_ in rw]
            with st.expander(f"🔁 {n} — {len(rw)} sprint(s) RSA"):
                st.dataframe(pd.DataFrame(rw), hide_index=True, use_container_width=True)
                figs = go.Figure()
                figs.add_trace(go.Bar(x=[r_["Sprint"] for r_ in rw],
                                      y=[v or 0 for v in ms_],
                                      name="P moy 6″", marker_color="#2563eb"))
                figs.add_trace(go.Scatter(x=[r_["Sprint"] for r_ in rw],
                                          y=[v or 0 for v in ps_],
                                          name="P pic", mode="lines+markers",
                                          line=dict(color="#dc2626", width=2)))
                figs.update_layout(template="plotly_white", height=320,
                                   yaxis_title="Puissance (W)")
                st.plotly_chart(figs, use_container_width=True)
                for r_ in rw:
                    if r_.get("Commentaire"):
                        st.caption(f"💬 {r_['Sprint']} — {r_['Commentaire']}")
                best = max(ms_) if any(ms_) else 0
                meanok = [v for v in ms_ if v]
                if best and meanok:
                    sdec = 100 * (1 - (sum(meanok) / len(meanok)) / best)
                    ver_ = ("🔁 Finisseur : il répète presque au même niveau — "
                            "c'est une arme en course par étapes." if sdec < 5
                            else "🟡 Correct : il perd de la force au fil des "
                                 "sprints — travaillable en RSA dans le plan."
                            if sdec <= 10 else
                            "🔴 Il s'essouffle vite : au-delà de 10 % de chute "
                            "moyenne — priorité RSA (intervalles courts répétés).")
                    st.caption(f"**S_dec : {sdec:.0f} %** — {ver_}")
        st.download_button("⬇️ RSA (CSV)",
                           pd.DataFrame(rows5).to_csv(index=False, sep=";",
                                                       decimal=",").encode("utf-8-sig"),
                           "tests_rsa.csv", "text/csv")
