import calendar as cal
import json
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.openrouter import chat, CURATED_MODELS, DEFAULT_MODEL
from core.config import openrouter_key

st.set_page_config(page_title="Plannif annuel — WT Performance Hub", page_icon="🗓️", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks"], key="pl_src")
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


def _weight(rid):
    a = next((x for x in riders if x["id"] == rid), {})
    try:
        return float(a.get("weight") or 0) or None
    except (TypeError, ValueError):
        return None


st.title("🗓️ Plannif annuel")
st.caption("Un calendrier par coureur (chacun ses courses et ses blocs). "
           "Tu planifies, l'IA propose — toi tu décides.")

plan_all = st.session_state.setdefault("plan", {})

_shared = st.session_state.get("rider_id")
_default = [_shared] if _shared in _keys else _keys[:1]
show_ids = st.sidebar.multiselect("Coureurs affichés au calendrier", _keys,
                                  default=_default, format_func=lambda x: labels[x],
                                  key="pl_show")
edit_id = st.sidebar.selectbox("Coureur à modifier", _keys,
                               index=_keys.index(_default[0]) if _default else 0,
                               format_func=lambda x: labels[x], key="pl_edit")
if show_ids and edit_id not in show_ids:
    show_ids = [edit_id] + list(show_ids)
st.session_state["rider_id"] = edit_id

RIDER_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2",
                "#be185d", "#65a30d", "#57534e", "#0f766e"]
color_of = {rid: RIDER_COLORS[i % len(RIDER_COLORS)] for i, rid in enumerate(show_ids)}
PRIO_COLORS = {"A": "#dc2626", "B": "#d97706", "C": "#94a3b8"}
PRIO_SHORT = {"A (objectif)": "A", "B (important)": "B", "C (préparation)": "C"}


def plan_for(rid):
    return plan_all.setdefault(rid, {"races": [], "blocks": []})


t_cal, t_edit, t_ia, t_save = st.tabs(
    ["📅 Calendrier", "✏️ Modifier", "🤖 Coach IA", "💾 Sauvegarde"])

with t_cal:
    today = date.today()
    cur = st.session_state.get("cal_month") or (today.year, today.month)
    nav1, nav2, nav3, nav4 = st.columns([1, 1, 4, 2])
    if nav1.button("◀", key="cal_prev"):
        y, mth = cur
        cur = (y - 1, 12) if mth == 1 else (y, mth - 1)
        st.session_state["cal_month"] = cur
    if nav2.button("▶", key="cal_next"):
        y, mth = cur
        cur = (y + 1, 1) if mth == 12 else (y, mth + 1)
        st.session_state["cal_month"] = cur
    if nav4.button("Aujourd'hui", key="cal_today"):
        cur = (today.year, today.month)
        st.session_state["cal_month"] = cur
    nav3.markdown(f"### {cal.month_name[cur[1]].capitalize()} {cur[0]}")
    st.session_state["cal_month"] = cur

    if not show_ids:
        st.info("Choisis au moins un coureur dans la barre de gauche.")
    else:
        leg = " · ".join(f'<span style="color:{color_of[rid]}">■</span> '
                         f'{labels[rid].split(" — ")[0]}' for rid in show_ids)
        st.markdown(leg, unsafe_allow_html=True)

        y, mth = cur
        first = date(y, mth, 1)
        ndays = cal.monthrange(y, mth)[1]
        off = first.weekday()
        rows = (off + ndays + 6) // 7

        def day_items(rid, d):
            items = []
            p = plan_for(rid)
            for r in p["races"]:
                if r["date"] == d.isoformat():
                    items.append(("race", r["name"],
                                  PRIO_COLORS.get(PRIO_SHORT.get(r["prio"], "C"), "#94a3b8")))
            for b in p["blocks"]:
                if b["start"] <= d.isoformat() <= b["end"]:
                    items.append(("block", b["label"], color_of[rid]))
            return items

        hdr = st.columns(7)
        for i, h in enumerate(["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]):
            hdr[i].markdown(f'<div style="text-align:center;font-weight:700;font-size:12px;'
                            f'color:#7a8194">{h}</div>', unsafe_allow_html=True)
        for w in range(rows):
            cols = st.columns(7)
            for dow in range(7):
                dnum = w * 7 + dow - off + 1
                if dnum < 1 or dnum > ndays:
                    continue
                d = date(y, mth, dnum)
                chips = ""
                for rid in show_ids:
                    for kind, label, col in day_items(rid, d):
                        nm = labels[rid].split(" — ")[0]
                        short = label if len(label) <= 14 else label[:13] + "…"
                        bg = col if kind == "block" else "transparent"
                        chips += (f'<div style="background:{bg};border:1px solid {col};'
                                  f'border-radius:3px;padding:1px 4px;font-size:10px;'
                                  f'margin-top:2px;color:{("#fff" if kind == "block" else col)}">'
                                  f'{"🏁 " if kind == "race" else ""}{short}'
                                  f'<span style="opacity:.6"> · {nm}</span></div>')
                is_today = (d == today)
                style = ("background:#fef9c3;border:1px solid #facc15;" if is_today
                         else "background:#f8fafc;border:1px solid #e2e8f0;")
                cols[dow].markdown(
                    f'<div style="{style}border-radius:5px;min-height:64px;padding:3px 5px">'
                    f'<div style="font-size:11px;font-weight:700;color:#334155">{dnum}</div>'
                    f'{chips}</div>', unsafe_allow_html=True)

        st.divider()
        st.subheader("Vue saison — blocs & courses par coureur")
        fig = go.Figure()
        all_dates = [pd.Timestamp(today)]
        for rid in show_ids:
            p = plan_for(rid)
            nm = labels[rid].split(" — ")[0]
            for b in p["blocks"]:
                s = pd.Timestamp(b["start"])
                e = pd.Timestamp(b["end"]) + pd.Timedelta(days=1)
                fig.add_trace(go.Bar(x=[(e - s).total_seconds() / 86400], base=[s],
                                     y=[nm], orientation="h",
                                     marker_color=color_of[rid], opacity=0.75,
                                     name=b["label"], showlegend=False,
                                     hovertemplate=f"{b['label']}<br>{b['start']} → "
                                                   f"{b['end']}<extra></extra>"))
                all_dates += [s, e]
            for r in p["races"]:
                s = pd.Timestamp(r["date"])
                fig.add_trace(go.Scatter(x=[s], y=[nm], mode="markers+text",
                                         marker=dict(symbol="diamond", size=13,
                                                     color=PRIO_COLORS.get(PRIO_SHORT.get(r["prio"], "C"), "#94a3b8")),
                                         text=[f"🏁 {r['name']}"], textposition="top center",
                                         name=r["name"], showlegend=False))
                all_dates.append(s)
        if len(fig.data):
            lo, hi = min(all_dates), max(all_dates)
            fig.update_xaxes(range=[lo - pd.Timedelta(days=10),
                                    hi + pd.Timedelta(days=10)])
        fig.update_layout(template="plotly_white", height=70 + 46 * max(len(show_ids), 1),
                          barmode="overlay", showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

with t_edit:
    plan = plan_for(edit_id)
    st.subheader(f"Courses — {labels[edit_id]}")
    with st.form("race_form", clear_on_submit=True):
        ra, rb, rc = st.columns([2, 1, 1])
        r_name = ra.text_input("Course (nom, lieu)")
        r_date = rb.date_input("Date", value=date.today() + timedelta(days=30))
        r_prio = rc.selectbox("Priorité", ["A (objectif)", "B (important)", "C (préparation)"])
        if st.form_submit_button("➕ Ajouter la course"):
            if r_name.strip():
                plan["races"].append({"date": r_date.isoformat(),
                                      "name": r_name.strip(), "prio": r_prio})
                plan["races"].sort(key=lambda x: x["date"])
                st.rerun()
    if plan["races"]:
        for i, r in enumerate(plan["races"]):
            ca, cb = st.columns([6, 1])
            d = pd.Timestamp(r["date"])
            j = (d - pd.Timestamp.today()).days
            ca.markdown(f"**{r['name']}** — {d.strftime('%d/%m/%Y')} "
                        f"({f'dans {j} j' if j >= 0 else 'passée'}) · {r['prio']}")
            if cb.button("🗑️", key=f"rm_race_{i}"):
                del plan["races"][i]
                st.rerun()
    else:
        st.info("Aucune course — l'IA s'appuie dessus pour construire les blocs.")

    st.divider()
    st.subheader(f"Blocs planifiés — {labels[edit_id]}")
    with st.form("block_form", clear_on_submit=True):
        ba, bb, bc = st.columns([1, 1, 2])
        b_start = ba.date_input("Début", value=date.today())
        b_end = bb.date_input("Fin", value=date.today() + timedelta(days=13))
        b_lbl = bc.text_input("Intitulé / focus (ex. Bloc seuil CLM 2 semaines)")
        if st.form_submit_button("➕ Ajouter le bloc"):
            if b_lbl.strip() and b_end >= b_start:
                plan["blocks"].append({"start": b_start.isoformat(), "end": b_end.isoformat(),
                                       "label": b_lbl.strip(), "src": "coach"})
                plan["blocks"].sort(key=lambda x: x["start"])
                st.rerun()
    if plan["blocks"]:
        for i, b in enumerate(plan["blocks"]):
            ca, cb = st.columns([6, 1])
            tag = "🤖" if b.get("src") == "ia" else "✍️"
            ca.markdown(f"{tag} **{b['label']}** — {pd.Timestamp(b['start']).strftime('%d/%m')} "
                        f"→ {pd.Timestamp(b['end']).strftime('%d/%m/%Y')}")
            if cb.button("🗑️", key=f"rm_blk_{i}"):
                del plan["blocks"][i]
                st.rerun()
    else:
        st.info("Aucun bloc — planifie toi-même ou demande au coach IA.")

with t_ia:
    plan = plan_for(edit_id)
    st.subheader(f"Propositions du coach IA — {labels[edit_id]}")
    model = st.selectbox("Modèle", CURATED_MODELS,
                         index=CURATED_MODELS.index(DEFAULT_MODEL), key="pl_model")
    if st.button("🤖 Demander des propositions", type="primary"):
        try:
            m = client.get_metrics(edit_id) or {}
        except Exception:
            m = {}
        try:
            pp = client.get_power_profile(edit_id) or {}
        except Exception:
            pp = {}
        try:
            load = client.get_daily_load(edit_id, 28) or []
        except Exception:
            load = []
        load_14 = [l for l in load if str(l.get("date", "")) >=
                   (pd.Timestamp.today() - pd.Timedelta(days=14)).strftime("%Y-%m-%d")]
        ctx = {
            "coureur": {"nom": labels[edit_id], "poids": _weight(edit_id)},
            "puissance": pp,
            "etat": {"ctl": m.get("ctl"), "atl": m.get("atl"),
                     "tsb": (float(m.get("ctl", 0) or 0) - float(m.get("atl", 0) or 0)),
                     "vfc": m.get("vfc_last"), "charge_7j": m.get("load_7d")},
            "charge_14j": [{"date": l.get("date"), "load": l.get("load")} for l in load_14],
            "courses": plan["races"],
            "blocs_deja_planifies": plan["blocks"],
        }
        prompt = (
            "Tu es l'entraîneur d'une équipe World Tour. Voici le contexte complet d'un "
            "coureur (JSON) :\n\n" + json.dumps(ctx, default=str, ensure_ascii=False) +
            "\n\nPropose 2 à 4 blocs d'entraînement pour les 6 prochaines semaines, en "
            "tenant compte : du passif du coureur (charge des 14 derniers jours, CTL/ATL/TSB), "
            "des courses à venir (priorité A = pic de forme), et des blocs déjà planifiés "
            "(ne les répète pas, ajuste-les si besoin).\n\n"
            'Réponds UNIQUEMENT avec un JSON valide, liste d\'objets :\n'
            '[{"start": "AAAA-MM-JJ", "end": "AAAA-MM-JJ", "label": "nom court du bloc", '
            '"focus": "détail des séances clés", "rationale": "pourquoi (1 phrase)"}]\n'
            "Pas de texte hors JSON."
        )
        with st.spinner("Le coach IA réfléchit…"):
            ok, content = chat(model, [{"role": "user", "content": prompt}],
                               openrouter_key(), max_tokens=2000)
        if not ok:
            st.error(f"OpenRouter : {content}")
        else:
            txt = content.strip()
            if txt.startswith("```"):
                txt = txt.strip("`")
                txt = txt[txt.find("["):] if "[" in txt else txt
            try:
                prop = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
            except Exception:
                prop = None
            if not prop:
                st.warning("L'IA n'a pas renvoyé de JSON exploitable — réponse brute :")
                st.markdown(content)
            else:
                st.session_state["plan_props"] = prop

    props = st.session_state.get("plan_props")
    if props:
        for i, b in enumerate(list(props)):
            with st.container(border=True):
                st.markdown(f"**{b.get('label', '?')}** — {b.get('start')} → {b.get('end')}")
                if b.get("focus"):
                    st.markdown(b["focus"])
                if b.get("rationale"):
                    st.caption(f"Pourquoi : {b['rationale']}")
                pa, pb = st.columns(2)
                if pa.button("✅ Adopter ce bloc", key=f"ad_{i}"):
                    plan["blocks"].append({"start": b.get("start"), "end": b.get("end"),
                                           "label": b.get("label", "Bloc IA"),
                                           "focus": b.get("focus", ""), "src": "ia"})
                    plan["blocks"].sort(key=lambda x: x["start"])
                    props.remove(b)
                    st.rerun()
                if pb.button("❌ Écarter", key=f"rj_{i}"):
                    props.remove(b)
                    st.rerun()

    st.divider()
    st.subheader("Discuter / affiner une proposition")
    fb = st.text_area("Ce que tu veux changer (ex. « le bloc 2 tombe sur le stage altitude, "
                      "décale-le et baisse le volume ») :")
    if st.button("💬 Envoyer au coach IA") and fb.strip():
        msgs = st.session_state.setdefault("plan_chat", [])
        if not msgs:
            msgs.append({"role": "user", "content":
                         "Contexte : nous construisons ensemble le plan annuel d'un coureur "
                         "World Tour. Tu me proposes des blocs en JSON, je te donne mes "
                         "ajustements, tu me renvoies le JSON complet mis à jour."})
        msgs.append({"role": "user", "content": fb.strip()})
        with st.spinner("…"):
            ok, content = chat(model, msgs, openrouter_key(), max_tokens=2000)
        if ok:
            msgs.append({"role": "assistant", "content": content})
            txt = content.strip()
            if "[" in txt and "]" in txt:
                try:
                    st.session_state["plan_props"] = json.loads(
                        txt[txt.find("["): txt.rfind("]") + 1])
                except Exception:
                    pass
            st.markdown(content)
        else:
            st.error(f"OpenRouter : {content}")

with t_save:
    st.subheader("Sauvegarde du plan (tous les coureurs)")
    st.caption("Le plan est gardé pendant ta session. Exporte-le en JSON pour le "
               "conserver, réimporte-le à la prochaine connexion (Streamlit Cloud ne "
               "permet pas d'écrire sur le serveur).")
    blob = json.dumps(plan_all, ensure_ascii=False, indent=1)
    st.download_button("⬇️ Exporter le plan (JSON)", blob, file_name="plannif_wt.json",
                       mime="application/json")
    up = st.file_uploader("Importer un plan (JSON)", type=["json"], key="plan_import")
    if up is not None and st.button("📥 Charger ce plan"):
        try:
            data = json.loads(up.getvalue().decode("utf-8"))
            if isinstance(data, dict):
                st.session_state["plan"] = data
                st.success("Plan importé.")
                st.rerun()
            else:
                st.error("JSON inattendu (dictionnaire attendu).")
        except Exception as e:
            st.error(f"Import impossible : {type(e).__name__} — {e}")
