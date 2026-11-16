import json
from datetime import date, timedelta

import pandas as pd
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
_shared = st.session_state.get("rider_id")
_idx = _keys.index(_shared) if _shared in _keys else 0
rider_id = st.sidebar.selectbox("Coureur", _keys, index=_idx, format_func=lambda x: labels[x],
                                key="pl_rider")
st.session_state["rider_id"] = rider_id
athlete = client.get_athlete(rider_id)
rname = athlete.get("name", rider_id)

st.title(f"🗓️ Plannif annuel — {rname}")
st.caption("Tes courses, tes blocs, et un coach IA qui propose — toi tu décides. "
           "Chaque proposition peut être adoptée, discutée ou supprimée.")

plan = st.session_state.setdefault("plan", {}).setdefault(rider_id, {"races": [], "blocks": []})

t_race, t_bloc, t_ia, t_save = st.tabs(
    ["🏁 Courses", "🧱 Blocs planifiés", "🤖 Coach IA", "💾 Sauvegarde"])


def _rm(table, idx, key):
    if st.button("🗑️", key=key):
        del plan[table][idx]
        st.rerun()


with t_race:
    st.subheader("Courses de la saison")
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
            _rm("races", i, f"rm_race_{i}")
    else:
        st.info("Aucune course. Ajoute les objectifs du coureur : l'IA s'en sert pour "
                "construire les blocs.")

with t_bloc:
    st.subheader("Blocs d'entraînement planifiés")
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
            _rm("blocks", i, f"rm_blk_{i}")
    else:
        st.info("Aucun bloc. Planifie toi-même ou demande au coach IA.")

with t_ia:
    st.subheader("Propositions du coach IA")
    model = st.selectbox("Modèle", CURATED_MODELS,
                         index=CURATED_MODELS.index(DEFAULT_MODEL), key="pl_model")
    if st.button("🤖 Demander des propositions", type="primary"):
        try:
            m = client.get_metrics(rider_id) or {}
        except Exception:
            m = {}
        try:
            pp = client.get_power_profile(rider_id) or {}
        except Exception:
            pp = {}
        try:
            load = client.get_daily_load(rider_id, 28) or []
        except Exception:
            load = []
        load_14 = [l for l in load if str(l.get("date", "")) >=
                   (pd.Timestamp.today() - pd.Timedelta(days=14)).strftime("%Y-%m-%d")]
        ctx = {
            "coureur": {"nom": rname, "role": athlete.get("role"),
                        "poids": athlete.get("weight")},
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
        for i, b in enumerate(props):
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
                    props.pop(i)
                    st.rerun()
                if pb.button("❌ Écarter", key=f"rj_{i}"):
                    props.pop(i)
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
    st.subheader("Sauvegarde du plan")
    st.caption("Le plan est gardé pendant ta session. Exporte-le en JSON pour le "
               "conserver, réimporte-le à la prochaine connexion (Streamlit Cloud ne "
               "permet pas d'écrire sur le serveur).")
    blob = json.dumps(st.session_state.get("plan", {}), ensure_ascii=False, indent=1)
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
