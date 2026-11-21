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

    st.subheader("2 \u00b7 Mesures prises pendant la s\u00e9ance")
    st.caption("Par bloc : lactate (piq\u00fbre juste apr\u00e8s), FC moyenne, puissance "
               "moyenne (compteur), distance et RPE. Le lactate de repos sert de base "
               "de comparaison. Enregistre par coureur \u2014 rien n'est perdu pendant la "
               "frappe, et les valeurs d\u00e9j\u00e0 saisies sont reprises au chargement.")
    sv1_rider = st.selectbox("Coureur", sel, key="sv1_rider")
    _prev_sv1 = st.session_state.get("sv1_data", {}).get(sv1_rider, {})
    _prev_p = _prev_sv1.get("p") or [None, None, None]
    _prev_km = _prev_sv1.get("km") or [None, None, None]
    _prev_rpe = _prev_sv1.get("rpe") or [None, None, None]
    _prev_lr = _prev_sv1.get("la_repos")
    with st.form("sv1_entry"):
        st.markdown(f"**{sv1_rider}** \u2014 repos : lactate saisi ci-dessous comme base")
        _lr = st.number_input("Lactate de repos (mmol/L)", 0.0, 20.0,
                              float(_prev_lr) if _prev_lr else 0.8, 0.1,
                              format="%.1f")
        _hcols = st.columns(3)
        for _i, _h in enumerate(["Bloc 1", "Bloc 2", "Bloc 3"]):
            _hcols[_i].caption(_h)
        _la_c = st.columns(3)
        la1 = _la_c[0].number_input("Lactate bloc 1 (mmol/L)", 0.0, 20.0, 1.5, 0.1,
                                    format="%.1f")
        la2 = _la_c[1].number_input("Lactate bloc 2 (mmol/L)", 0.0, 20.0, 1.5, 0.1,
                                    format="%.1f")
        la3 = _la_c[2].number_input("Lactate bloc 3 (mmol/L)", 0.0, 20.0, 1.5, 0.1,
                                    format="%.1f")
        _fc_c = st.columns(3)
        fc1 = _fc_c[0].number_input("FC moy. bloc 1 (bpm)", 80, 230, 150)
        fc2 = _fc_c[1].number_input("FC moy. bloc 2 (bpm)", 80, 230, 150)
        fc3 = _fc_c[2].number_input("FC moy. bloc 3 (bpm)", 80, 230, 150)
        _pw_c = st.columns(3)
        pw1 = _pw_c[0].number_input("P moy. bloc 1 (W)", 0, 800,
                                    int(_prev_p[0]) if _prev_p[0] else 0)
        pw2 = _pw_c[1].number_input("P moy. bloc 2 (W)", 0, 800,
                                    int(_prev_p[1]) if _prev_p[1] else 0)
        pw3 = _pw_c[2].number_input("P moy. bloc 3 (W)", 0, 800,
                                    int(_prev_p[2]) if _prev_p[2] else 0)
        _km_c = st.columns(3)
        km1 = _km_c[0].number_input("Distance bloc 1 (km)", 0.0, 100.0,
                                    float(_prev_km[0]) if _prev_km[0] else 0.0,
                                    0.1, format="%.1f")
        km2 = _km_c[1].number_input("Distance bloc 2 (km)", 0.0, 100.0,
                                    float(_prev_km[1]) if _prev_km[1] else 0.0,
                                    0.1, format="%.1f")
        km3 = _km_c[2].number_input("Distance bloc 3 (km)", 0.0, 100.0,
                                    float(_prev_km[2]) if _prev_km[2] else 0.0,
                                    0.1, format="%.1f")
        _rp_c = st.columns(3)
        rpe1 = _rp_c[0].number_input("RPE bloc 1 (/10)", 0.0, 10.0,
                                     float(_prev_rpe[0]) if _prev_rpe[0] else 0.0,
                                     0.5, format="%.1f")
        rpe2 = _rp_c[1].number_input("RPE bloc 2 (/10)", 0.0, 10.0,
                                     float(_prev_rpe[1]) if _prev_rpe[1] else 0.0,
                                     0.5, format="%.1f")
        rpe3 = _rp_c[2].number_input("RPE bloc 3 (/10)", 0.0, 10.0,
                                     float(_prev_rpe[2]) if _prev_rpe[2] else 0.0,
                                     0.5, format="%.1f")
        if st.form_submit_button("\ud83d\udcbe Enregistrer ces donn\u00e9es"):
            st.session_state.setdefault("sv1_data", {})[sv1_rider] = {
                "la": [la1, la2, la3], "fc": [fc1, fc2, fc3],
                "p": [pw1 or None, pw2 or None, pw3 or None],
                "km": [km1 or None, km2 or None, km3 or None],
                "rpe": [rpe1 or None, rpe2 or None, rpe3 or None],
                "la_repos": _lr,
                "group": group_of.get(sv1_rider, "\u2014")}


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
                for i in range(3):
                    la_i = d["la"][i]
                    fc_i = d["fc"][i]
                    p_i = (d.get("p") or [None] * 3)[i]
                    km_i = (d.get("km") or [None] * 3)[i]
                    rpe_i = (d.get("rpe") or [None] * 3)[i]
                    bits = [f"lactate **{la_i:.1f} mmol/L** — "
                            f"{sv1_block_verdict(la_i)}",
                            f"FC moy. {fc_i:.0f} bpm"]
                    if p_i:
                        bits.append(f"P moy. {p_i:.0f} W "
                                    f"({p_i / weight_of(n):.1f} W/kg)")
                    if km_i:
                        bits.append(f"{km_i:.1f} km")
                    if rpe_i:
                        bits.append(f"RPE {rpe_i:.0f}/10")
                    st.markdown(f"**Bloc {i + 1}** : " + " · ".join(bits))
                    if _trow is not None:
                        lo_f = _num(_trow.get("FC basse"))
                        hi_f = _num(_trow.get("FC haute"))
                        if lo_f and hi_f and not (lo_f - 3 <= fc_i <= hi_f + 2):
                            st.caption("🟠 FC hors fenêtre cible "
                                       f"({lo_f:.0f}–{hi_f:.0f} bpm)")
                        pl = _num(_trow.get("P basse (W)"))
                        ph = _num(_trow.get("P haute (W)"))
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
            la = _num(r0.get("Lactate (mmol/L)"))
            dur = _num(r0.get("Durée (min)"))
            rpe = _num(r0.get("RPE (/10)"))
            prv = rev_power(p, fc, _num(ft.get("fcmax")))
            paliers_raw.append({"Palier": i + 1, "Durée (min)": dur,
                                "P moy. (W)": p, "FC moy. (bpm)": fc,
                                "Lactate (mmol/L)": la, "RPE": rpe,
                                "P révisée (W)": prv})
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

    opts = list(sel) + [n for n in fdata if n not in sel]
    field_rider = st.selectbox("Coureur à saisir / modifier", opts,
                               key="field_rider")

    ft = fdata.get(field_rider, {})
    _pals_prev = ft.get("paliers")
    if not isinstance(_pals_prev, pd.DataFrame) or _pals_prev.empty:
        _pals_prev = pd.DataFrame({
            "Durée (min)": [4.0, 4.0, 4.0, 4.0],
            "P moy. (W)": [None] * 4, "FC moy. (bpm)": [None] * 4,
            "Lactate (mmol/L)": [None] * 4, "RPE (/10)": [None] * 4})
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
            st.markdown("**Paliers** — 1 ligne = 1 effort, de plus en plus fort. "
                        "Ils sont triés par puissance pour l'analyse.")
            pals = st.data_editor(
                _pals_prev.reindex(columns=["Durée (min)", "P moy. (W)",
                                            "FC moy. (bpm)", "Lactate (mmol/L)",
                                            "RPE (/10)"]),
                key=f"field_paliers_{abs(hash(field_rider)) % 10 ** 8}",
                hide_index=True, use_container_width=True, num_rows="dynamic",
                column_config={
                    "Durée (min)": st.column_config.NumberColumn(
                        "Durée (min)", min_value=0.0, max_value=120.0, step=0.5),
                    "P moy. (W)": st.column_config.NumberColumn(
                        "P moy. (W)", min_value=50, max_value=1200, step=1),
                    "FC moy. (bpm)": st.column_config.NumberColumn(
                        "FC moy. (bpm)", min_value=80, max_value=230, step=1),
                    "Lactate (mmol/L)": st.column_config.NumberColumn(
                        "Lactate (mmol/L)", min_value=0.0, max_value=20.0, step=0.1,
                        format="%.1f"),
                    "RPE (/10)": st.column_config.NumberColumn(
                        "RPE (/10)", min_value=0.0, max_value=10.0, step=0.5,
                        format="%.1f"),
                })
            saved_ft = st.form_submit_button(
                f"💾 Enregistrer le test de {field_rider}")
    except Exception:
        st.warning("Tableau réinitialisé (état incompatible détecté).")
        st.rerun()
    if saved_ft:
        fdata[field_rider] = {"date": f_date, "lieu": f_lieu, "fcmax": f_fcmax,
                              "poids": f_kg, "la_repos": f_lrep,
                              "paliers": pals,
                              "group": group_of.get(field_rider, "—")}
        st.session_state["field_report"] = {}
        st.success(f"Test de {field_rider} enregistré — paliers triés par "
                   "puissance pour l'analyse.")

    _entered_f = [n for n in fdata if isinstance(fdata[n].get("paliers"),
                                                 pd.DataFrame)]
    if _entered_f:
        _rem = st.multiselect("🗑️ Retirer des tests enregistrés", _entered_f,
                              key="field_remove")
        if st.button("♻️ Retirer ces tests", key="field_rm_btn") and _rem:
            for _nm in _rem:
                fdata.pop(_nm, None)
            st.session_state["field_report"] = {}
            st.rerun()

    if st.button("📊 Générer les rapports — tous les tests saisis",
                 type="primary", key="field_report_btn"):
        st.session_state["field_report"] = {}
        with st.spinner("Analyse de tous les tests…"):
            for _n in _entered_f:
                _ready = sum(1 for _, _r in fdata[_n]["paliers"].iterrows()
                             if _num(_r.get("Lactate (mmol/L)")))
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
                     "Lactate (mmol/L)": p["Lactate (mmol/L)"], "RPE": p["RPE"],
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
