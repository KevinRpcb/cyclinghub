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
from core.powermodel import CP_ZONE_PCTS, cp_from_5_20, cp_zone_table

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
           "Test SV1 3×15' → zone cible en amont, lactates par bloc, analyse par coureur "
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
    "field": ("🏁 Lactate Terrain",
              "Collecte de test de terrain : puissance révisée, code couleur suspects, "
              "W/kg vs durée",
              "En usage"),
    "cp": ("⏱️ 5′ & 20′ CP",
           "Deux efforts à bloc → CP/W′ à 2 points, 6 zones d'entraînement",
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
             "FC (bpm)": [None] * 6, "Lactate (mmol/L)": [None] * 6})
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
                                    })
            b1, b2 = st.columns(2)
            saved = b1.form_submit_button("💾 Enregistrer les paliers")
            gen = b2.form_submit_button("📊 Générer le rapport",
                                        type="primary")
    except Exception:
        st.session_state[steps_key] = pd.DataFrame(
            {"Puissance (W)": [None], "FC (bpm)": [None], "Lactate (mmol/L)": [None]})
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
        seuils_csv = pd.DataFrame(
            [{"Coureur": r["name"], "Groupe": r["group"],
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
    st.subheader("1 · Zone cible en amont")
    st.caption("3 × 15' calés entre SV1 et SV2 : FC entre SV1+2 bpm et SV2−10 bpm, "
               "puissance entre P@SV1 et la moitié SV1→SV2. Sans test lactate saisi : "
               "repli 78–85 % FCmax et 70–85 % CP. Tout est modifiable à la main.")
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
            base = "test lactate"
        else:
            lo_hr, hi_hr = 0.78 * hrmax, 0.85 * hrmax
            lo_p = 0.70 * cp if cp else None
            hi_p = 0.85 * cp if cp else None
            base = "repli %HRmax / %CP"
        tgt_rows.append({"Coureur": n, "Groupe": group_of.get(n, "—"),
                         "FC basse": round(lo_hr), "FC haute": round(hi_hr),
                         "P basse (W)": round(lo_p) if lo_p else None,
                         "P haute (W)": round(hi_p) if hi_p else None,
                         "Ancrage": base})
    tgt_key = f"sv1_targets_{abs(hash(tuple(sel))) % 10 ** 8}"
    tdf = st.data_editor(pd.DataFrame(tgt_rows), key=tgt_key, hide_index=True,
                         use_container_width=True, num_rows="dynamic",
                         column_config={
                             "Coureur": st.column_config.TextColumn("Coureur", disabled=True),
                             "Groupe": st.column_config.TextColumn("Groupe", disabled=True),
                             "FC basse": st.column_config.NumberColumn(min_value=80, max_value=230),
                             "FC haute": st.column_config.NumberColumn(min_value=80, max_value=230),
                             "P basse (W)": st.column_config.NumberColumn(min_value=50, max_value=700),
                             "P haute (W)": st.column_config.NumberColumn(min_value=50, max_value=700),
                         })
    st.download_button("⬇️ Cibles 3×15' (CSV)",
                       tdf.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       "cibles_sv1_3x15.csv", "text/csv")

    st.subheader("2 · Lactates après chaque bloc de 15'")
    sv1_rider = st.selectbox("Coureur", sel, key="sv1_rider")
    with st.form("sv1_entry"):
        e1, e2, e3 = st.columns(3)
        la1 = e1.number_input("Lactate bloc 1 (mmol/L)", 0.0, 20.0, 1.5, 0.1, format="%.1f")
        la2 = e2.number_input("Lactate bloc 2", 0.0, 20.0, 1.5, 0.1, format="%.1f")
        la3 = e3.number_input("Lactate bloc 3", 0.0, 20.0, 1.5, 0.1, format="%.1f")
        f1, f2, f3 = st.columns(3)
        fc1 = f1.number_input("FC moy. bloc 1", 80, 230, 150)
        fc2 = f2.number_input("FC moy. bloc 2", 80, 230, 150)
        fc3 = f3.number_input("FC moy. bloc 3", 80, 230, 150)
        if st.form_submit_button("💾 Enregistrer ces lactates"):
            st.session_state.setdefault("sv1_data", {})[sv1_rider] = {
                "la": [la1, la2, la3], "fc": [fc1, fc2, fc3],
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
              "B1": data[n]["la"][0], "B2": data[n]["la"][1], "B3": data[n]["la"][2],
              "Dérive B1→B3": f"{data[n]['la'][2] - data[n]['la'][0]:+.1f}",
              "Verdict": sv1_global(data[n])} for n in entered]),
            hide_index=True, use_container_width=True)
        for n in entered:
            d = data[n]
            trend = sv1_trend(d["la"])
            with st.expander(f"{n} — {d['group']}"):
                for i, (la, fc) in enumerate(zip(d["la"], d["fc"])):
                    st.markdown(f"**Bloc {i + 1}** : lactate **{la:.1f} mmol/L** — "
                                f"{sv1_block_verdict(la)} · FC moyenne {fc:.0f} bpm")
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
                agg.append({"Groupe": g, "Coureurs": len(members),
                            "Lactate moy. B1": round(float(las[:, 0].mean()), 2),
                            "Lactate moy. B2": round(float(las[:, 1].mean()), 2),
                            "Lactate moy. B3": round(float(las[:, 2].mean()), 2),
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
                               pd.DataFrame([{"Coureur": n, "Groupe": data[n]["group"],
                                              "Lactate B1": data[n]["la"][0],
                                              "Lactate B2": data[n]["la"][1],
                                              "Lactate B3": data[n]["la"][2],
                                              "FC moy. B1": data[n]["fc"][0],
                                              "FC moy. B2": data[n]["fc"][1],
                                              "FC moy. B3": data[n]["fc"][2],
                                              "Verdict": sv1_global(data[n])}
                                             for n in pool]).to_csv(
                                   index=False, sep=";", decimal=",").encode("utf-8-sig"),
                               "resultats_sv1_3x15.csv", "text/csv")
    else:
        st.info("Enregistre les lactates d'un coureur ci-dessus pour lancer les analyses.")


# ═══════════════════════════════ TEST DE TERRAIN — COLLECTE ═══════════════════════════════
elif tool == "field":
    st.subheader("Collecte test de terrain")
    st.caption(
        "Une ligne par coureur et par test. Puissance et FC saisies = **moyennes sur la montée**. La **puissance révisée** applique la formule de "
        "l'équipe : P révisée = P − ((FC − 0,8 × FCmax) / FCmax) × P — la puissance est "
        "ajustée proportionnellement à l'écart de la FC par rapport à 80 % de la FCmax "
        "(au-dessus → révisée à la baisse, en dessous → à la hausse). "
        "🔴 rouge = perf suspecte (à vérifier avant d'exploiter) · "
        "🟠 orange = point de vigilance.")

    FIELD_COLS = ["Nom", "Groupe", "Date", "Lieu", "Durée (min)", "Puissance moy. (W)",
                  "FC moy. (bpm)", "FCmax (bpm)", "Récup 1' (bpm)", "Lactate (mmol/L)",
                  "RPE", "Poids (kg)"]
    field_key = "field_collect"
    if not isinstance(st.session_state.get(field_key), pd.DataFrame) or \
            st.session_state[field_key].empty:
        st.session_state[field_key] = pd.DataFrame(
            [{"Nom": "", "Groupe": GROUPS[0], "Date": "", "Lieu": "",
              "Durée (min)": None, "Puissance moy. (W)": None, "FC moy. (bpm)": None,
              "FCmax (bpm)": None, "Récup 1' (bpm)": None, "Lactate (mmol/L)": None,
              "RPE": None, "Poids (kg)": None}])
    st.caption("Saisis puis clique sur 💾 Enregistrer — rien n'est perdu "
               "pendant la frappe.")
    try:
        with st.form("field_form", clear_on_submit=False):
            fdf = st.data_editor(
                st.session_state[field_key].reindex(columns=FIELD_COLS), key="field_ed",
                hide_index=True, use_container_width=True, num_rows="dynamic",
                column_config={
                    "Nom": st.column_config.TextColumn("Nom", required=True),
                    "Groupe": st.column_config.SelectboxColumn("Groupe", options=GROUPS,
                                                               required=True),
                    "Date": st.column_config.TextColumn("Date", help="JJ/MM/AAAA"),
                    "Lieu": st.column_config.TextColumn("Lieu"),
                    "Durée (min)": st.column_config.NumberColumn("Durée (min)",
                                                                 min_value=0.0,
                                                                 max_value=600.0,
                                                                 step=0.5),
                    "Puissance moy. (W)": st.column_config.NumberColumn(
                        "Puissance moy. (W)", min_value=50, max_value=1200, step=1),
                    "FC moy. (bpm)": st.column_config.NumberColumn(
                        "FC moy. (bpm)", min_value=80, max_value=230, step=1),
                    "FCmax (bpm)": st.column_config.NumberColumn("FCmax (bpm)",
                                                                 min_value=120,
                                                                 max_value=230, step=1),
                    "Récup 1' (bpm)": st.column_config.NumberColumn(
                        "Récup 1' (bpm)", min_value=60, max_value=230, step=1),
                    "Lactate (mmol/L)": st.column_config.NumberColumn(
                        "Lactate (mmol/L)", min_value=0.0, max_value=20.0, step=0.1,
                        format="%.1f"),
                    "RPE": st.column_config.NumberColumn("RPE", min_value=1.0,
                                                         max_value=10.0, step=0.5),
                    "Poids (kg)": st.column_config.NumberColumn("Poids (kg)",
                                                                min_value=30.0,
                                                                max_value=120.0,
                                                                step=0.5),
                })
            saved_f = st.form_submit_button("💾 Enregistrer la collecte")
    except Exception:
        st.session_state[field_key] = pd.DataFrame(
            [{c: None for c in FIELD_COLS}])
        st.warning("Tableau réinitialisé (état incompatible détecté).")
        st.rerun()
    if saved_f:
        st.session_state[field_key] = fdf

    RED_FLAGS = {"FC ≥ FCmax", "W/kg atypique vs groupe"}

    def field_flags(p, p_rev, fc, fcmax, rec, la, dur, wkg, grp_vals):
        flags = []
        if fc and fcmax and fc >= fcmax:
            flags.append("FC ≥ FCmax")
        if fc and fcmax and fc < 0.60 * fcmax:
            flags.append("FC très basse")
        if p and p_rev and abs(p_rev - p) / p > 0.10:
            flags.append("révision > 10 %")
        if fc and rec and 0 < fc - rec < 15:
            flags.append("récup 1' faible")
        if la is None:
            flags.append("lactate manquant")
        if dur and (dur < 3 or dur > 90):
            flags.append("durée inhabituelle")
        if wkg and len(grp_vals) >= 3:
            arr = np.array(grp_vals, dtype=float)
            sd = float(arr.std())
            if sd > 0 and abs(wkg - float(arr.mean())) > 2 * sd:
                flags.append("W/kg atypique vs groupe")
        return flags

    def rev_power(p, fc, fcmax):
        if not (p and fc and fcmax):
            return None
        return p - ((fc - 0.8 * fcmax) / fcmax) * p

    def field_computed(df):
        rows = []
        raw = df.to_dict("records")
        for r in raw:
            nom = str(r.get("Nom") or "").strip()
            if not nom or nom.lower() == "nan":
                continue
            p = _num(r.get("Puissance moy. (W)"))
            fc = _num(r.get("FC moy. (bpm)"))
            fcmax = _num(r.get("FCmax (bpm)"))
            rec = _num(r.get("Récup 1' (bpm)"))
            la = _num(r.get("Lactate (mmol/L)"))
            dur = _num(r.get("Durée (min)"))
            kg = _num(r.get("Poids (kg)"))
            p_rev = rev_power(p, fc, fcmax)
            wkg = (p_rev / kg) if (p_rev and kg) else None
            grp = str(r.get("Groupe") or "—")
            grp_vals = []
            for x in raw:
                if str(x.get("Groupe") or "—") != grp or \
                        not str(x.get("Nom") or "").strip():
                    continue
                xv = rev_power(_num(x.get("Puissance moy. (W)")), _num(x.get("FC moy. (bpm)")),
                               _num(x.get("FCmax (bpm)")))
                xkg = _num(x.get("Poids (kg)"))
                if xv and xkg:
                    grp_vals.append(xv / xkg)
            flags = field_flags(p, p_rev, fc, fcmax, rec, la, dur, wkg, grp_vals)
            rows.append({
                "Nom": nom, "Groupe": grp, "Date": str(r.get("Date") or ""),
                "Lieu": str(r.get("Lieu") or ""),
                "Durée (min)": dur, "Puissance moy. (W)": p,
                "P révisée (W)": round(p_rev) if p_rev else None,
                "FC moy. (bpm)": fc, "FCmax (bpm)": fcmax,
                "80% FCmax": round(0.8 * fcmax) if fcmax else None,
                "Récup 1' (bpm)": rec, "Lactate (mmol/L)": la, "RPE": _num(r.get("RPE")),
                "W/kg (révisée)": round(wkg, 2) if wkg else None,
                "⚠️ Suspect": " · ".join(flags), "_niveau":
                    "rouge" if RED_FLAGS & set(flags) else ("orange" if flags else "")})
        return pd.DataFrame(rows)

    disp = field_computed(fdf)
    if disp.empty:
        st.info("Ajoute une ligne (au moins un Nom) — puissance révisée, W/kg et contrôles "
                "se calculent automatiquement.")
    else:
        def _paint(row):
            color = ("#fee2e2" if row["_niveau"] == "rouge"
                     else "#fef3c7" if row["_niveau"] else "")
            return [f"background-color:{color}"] * len(row) if color else [""] * len(row)

        st.dataframe(disp.style.apply(_paint, axis=1).hide(axis="index"),
                     use_container_width=True)

        st.subheader("W/kg vs durée — par groupe")
        grp_filter = st.selectbox("Groupe affiché", ["Tous les groupes"] + GROUPS,
                                  key="field_grp")
        plot_df = disp[disp["W/kg (révisée)"].notna() & disp["Durée (min)"].notna()]
        if grp_filter != "Tous les groupes":
            plot_df = plot_df[plot_df["Groupe"] == grp_filter]
        if plot_df.empty:
            st.info("Renseigne durée, puissance, FCmax et poids pour alimenter le graphe.")
        else:
            GROUP_COLORS = {"WT Hommes": "#dc2626", "WT Femmes": "#db2777",
                            "Conti Hommes": "#2563eb", "Conti Femmes": "#16a34a"}
            fig = go.Figure()
            for g in sorted(plot_df["Groupe"].unique()):
                sub = plot_df[plot_df["Groupe"] == g]
                symbols = ["diamond-open" if s else "circle"
                           for s in sub["⚠️ Suspect"].astype(bool)]
                fig.add_trace(go.Scatter(
                    x=sub["Durée (min)"], y=sub["W/kg (révisée)"],
                    mode="markers+text", name=g, text=sub["Nom"],
                    textposition="top center",
                    marker=dict(size=11,
                                color=GROUP_COLORS.get(g, "#64748b"),
                                symbol=symbols),
                    customdata=sub["⚠️ Suspect"],
                    hovertemplate="%{text}<br>%{x:.0f} min · %{y:.2f} W/kg"
                                  "<extra>%{customdata}</extra>"))
            fig.update_layout(template="plotly_white", height=380,
                              xaxis_title="Durée (min)", yaxis_title="W/kg (puissance révisée)",
                              legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                          xanchor="right", x=1))
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Losange ouvert = perf suspecte.")

        exp_rows = disp.drop(columns=["_niveau"])
        st.download_button("⬇️ Collecte test de terrain (CSV)",
                           exp_rows.to_csv(index=False, sep=";", decimal=",")
                           .encode("utf-8-sig"),
                           "collecte_test_terrain.csv", "text/csv")


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
        for _, row in cdisp.iterrows():
            cp = _num(row["CP (W)"])
            if not cp:
                continue
            fc20 = _num(row["FC seuil ≈ FC 20′"])
            with st.expander(f"{row['Nom']} — {row['Groupe']} · CP {cp:.0f} W"
                             + (f" ({row['CP (W/kg)']:.2f} W/kg)"
                                if row["CP (W/kg)"] else "")):
                zrows = []
                for (lbl, lo_pct, hi_pct), (_, lo, hi) in zip(
                        CP_ZONE_PCTS, cp_zone_table(cp)):
                    zrows.append({
                        "Zone": lbl,
                        "Bas (W)": round(lo) if lo else "—",
                        "Haut (W)": round(hi) if hi else "ouvert",
                        "Bas (bpm)": round(lo_pct * fc20) if (lo_pct and fc20) else "—",
                        "Haut (bpm)": round(hi_pct * fc20) if (hi_pct and fc20) else
                        ("ouvert" if hi_pct is None and fc20 else "—")})
                st.dataframe(pd.DataFrame(zrows), hide_index=True,
                             use_container_width=True)
                if fc20:
                    st.caption("Zones FC en % de la FC au seuil (≈ FC moy. 20′) — "
                               "ajuste si tu connais la FCmax du coureur.")

        st.subheader("CP (W/kg) par coureur")
        bar_df = cdisp[cdisp["CP (W/kg)"].notna()].sort_values(
            "CP (W/kg)", ascending=False)
        if not bar_df.empty:
            GROUP_COLORS = {"WT Hommes": "#dc2626", "WT Femmes": "#db2777",
                            "Conti Hommes": "#2563eb", "Conti Femmes": "#16a34a"}
            fig = go.Figure()
            for g in sorted(bar_df["Groupe"].unique()):
                sub = bar_df[bar_df["Groupe"] == g]
                fig.add_trace(go.Bar(x=sub["Nom"], y=sub["CP (W/kg)"], name=g,
                                     marker_color=GROUP_COLORS.get(g, "#64748b")))
            fig.update_layout(template="plotly_white", barmode="group", height=340,
                              yaxis_title="CP (W/kg)",
                              legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                          xanchor="right", x=1))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Renseigne la puissance 20′ (et le poids) pour afficher le graphe.")

        st.download_button("⬇️ Test 5′ & 20′ (CSV)",
                           cdisp.drop(columns=["_niveau"]).to_csv(
                               index=False, sep=";", decimal=",").encode("utf-8-sig"),
                           "test_5_20_cp.csv", "text/csv")
