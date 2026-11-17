import streamlit as st

from core.auth import gate
from core.config import openrouter_key
from core.openrouter import chat, list_models, free_models, multi_consult, synthesize, CURATED_MODELS
from core.pubmed import feed, search
from core.vekta import VektaClient

st.set_page_config(page_title="Labo Scientifique — WT Performance Hub", page_icon="🔬", layout="wide")
gate()

st.title("🔬 Labo Scientifique — veille multi-IA & PubMed")
st.caption("Sonde plusieurs IA en parallèle via OpenRouter, croise avec la littérature PubMed, "
           "obtiens une synthèse critique unique. Point central de la veille du staff.")

key = openrouter_key()
client = VektaClient()

# Modèles gratuits détectés automatiquement (la liste d'OpenRouter change souvent)
if "lab_free_models" not in st.session_state:
    ok_free, free_ids = free_models()
    st.session_state["lab_free_models"] = free_ids if ok_free else []
free_ids = st.session_state["lab_free_models"]
model_options = list(dict.fromkeys(free_ids + CURATED_MODELS))
default_models = (free_ids[:3] or CURATED_MODELS[:3])
fallback_model = (free_ids or CURATED_MODELS)[0]

# ── Diagnostic clé ───────────────────────────────────────────────────────
with st.expander("🔑 Diagnostic OpenRouter", expanded=not key):
    if st.button("Vérifier la clé et lister les modèles"):
        ok, res = list_models(key)
        if ok:
            st.success(f"Clé valide — {len(res)} modèles accessibles.")
            free = [m for m in res if ":free" in m]
            st.caption(f"Dont {len(free)} modèles gratuits. Exemples : {', '.join(free[:8]) or '—'}")
        else:
            st.error(f"Clé OpenRouter : {res}")
    if not key:
        st.info("Ajoutez OPENROUTER_API_KEY dans les Secrets Streamlit (jamais dans le code).")

# ── Contexte équipe injecté dans les prompts ─────────────────────────────
riders = client.get_athletes()
team_ctx = []
for a in riders[:3]:
    m = client.get_metrics(a["id"])
    team_ctx.append({"coureur": a.get("name"), "profil": a.get("role"),
                     "vfc": m.get("vfc_last"), "baseline": m.get("vfc_baseline"),
                     "decouplage": m.get("decouplage_cardio"), "tsb": round(
                         float(m.get("ctl", 0)) - float(m.get("atl", 0)), 1)})

SYSTEM = (
    "Tu es physiologiste World Tour, docteur en physiologie de l'exercice. "
    "Réponds en français, de façon précise, chiffrée, appuyée sur la littérature "
    "quand c'est possible, et directement opérationnelle pour un staff d'équipe. "
    "Contexte de l'équipe (données récentes) : " + str(team_ctx)
)

tab1, tab2, tab3 = st.tabs(["📚 Veille PubMed critique", "💡 Brainstorming", "🧭 Fil de veille (auto)"])

with tab1:
    models = st.multiselect(
        f"IA à sonder ({len(free_ids)} gratuites détectées automatiquement)",
        model_options, default=default_models, key="lab_models")
    q = st.text_input("Mots-clés PubMed (anglais)", "heat acclimation cycling time trial")
    if st.button("🚀 Lancer l'analyse multi-IA", type="primary"):
        with st.spinner("Recherche PubMed…"):
            abstracts = search(q, retmax=8)
        if abstracts.startswith("Erreur") or "Aucun" in abstracts[:30]:
            st.error(abstracts)
        else:
            prompt = (
                "Voici des abstracts PubMed récents :\n\n" + abstracts[:12000] +
                "\n\nStructure ta réponse :\n1. CONSENSUS ACTUEL\n2. CRITIQUE MÉTHODOLOGIQUE "
                "(validité externe pour des athlètes élites World Tour : échantillons, durées, "
                "niveaux de performance)\n3. FORCE DES CONCLUSIONS\n4. APPLICATION TERRAIN "
                "(protocole concret, valeurs cibles, pièges)"
            )
            results = multi_consult(models, SYSTEM, prompt, key)
            for r in results:
                with st.expander(f"🤖 {r['model']}", expanded=r["ok"]):
                    if r["ok"]:
                        st.markdown(r["content"])
                    else:
                        st.error(r["content"])
            if any(r["ok"] for r in results) and models:
                with st.spinner("Synthèse croisée…"):
                    ok, content = synthesize(results, SYSTEM, prompt, key, models[0])
                st.subheader("⚖️ Synthèse des IA")
                st.markdown(content if ok else f"❌ {content}")
            with st.expander("Voir les abstracts bruts"):
                st.text(abstracts)

with tab2:
    sujet = st.text_area("Problématique du staff :",
                         "Propose 3 séances spécifiques pour corriger un découplage mécanique "
                         "observé chez un grimpeur à J-10 d'un grand tour.")
    if st.button("🧠 Consulter les IA", type="primary"):
        results = multi_consult(models or [fallback_model], SYSTEM, sujet, key)
        for r in results:
            with st.expander(f"🤖 {r['model']}", expanded=r["ok"]):
                st.markdown(r["content"] if r["ok"] else f"❌ {r['content']}")

DEFAULT_WATCH = [
    "cycling performance physiology",
    "endurance training elite cyclists",
    "heat acclimation endurance performance",
    "altitude training endurance athletes",
    "carbohydrate fueling endurance performance",
]


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def _feed_cached(queries: tuple, days: int, retmax: int, _bump: int = 0):
    return feed(list(queries), days=days, retmax=retmax)


with tab3:
    st.subheader("📰 Fil de veille scientifique automatique")
    st.caption("Articles PubMed les plus récents de ton domaine, triés par date. "
               "Le fil se met à jour tout seul chaque semaine. L'IA synthétise "
               "et propose de nouveaux sujets.")
    if "watch_queries" not in st.session_state:
        st.session_state["watch_queries"] = list(DEFAULT_WATCH)
    queries = st.session_state["watch_queries"]
    with st.expander("⚙️ Sujets surveillés (requêtes PubMed)"):
        new_q = st.text_input("Ajouter un sujet (requête anglaise)", key="watch_add")
        if st.button("➕ Ajouter au fil") and new_q.strip():
            if new_q.strip() not in st.session_state["watch_queries"]:
                st.session_state["watch_queries"].append(new_q.strip())
            st.rerun()
        for i, sq in enumerate(list(queries)):
            c1, c2 = st.columns([10, 1])
            c1.text(f"• {sq}")
            if c2.button("✖", key=f"watch_del_{abs(hash(sq)) % 10 ** 8}"):
                st.session_state["watch_queries"].pop(i)
                st.rerun()
    wb1, wb2 = st.columns(2)
    days = wb1.slider("Fenêtre de recherche (jours)", 14, 180, 60)
    if wb2.button("🔄 Actualiser maintenant"):
        _feed_cached.clear()
        st.rerun()
    arts = _feed_cached(tuple(queries), days, 6) if queries else []
    if not arts:
        st.info("Aucun article récent pour ces sujets — élargis la fenêtre ou "
                "ajoute des sujets.")
    else:
        st.caption(f"{len(arts)} articles des {days} derniers jours — du plus récent "
                   "au plus ancien.")
        for a in arts:
            if a["pmid"] is None:
                st.error(a["title"])
                continue
            with st.expander(f"📄 {a['date']} — {a['title'][:90]}"):
                st.markdown(
                    f"**[{a['title']}](https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/)**")
                st.caption(f"{a['journal']} · {a['date']} · sujet : {a['query']}")
                st.text(a["abstract"][:1600])
        st.subheader("🧠 Synthèse IA du fil")
        if st.button("🔬 Analyser le fil avec les IA", type="primary"):
            abstracts = "\n\n".join(
                f"[{a['query']}] {a['title']} ({a['journal']}, {a['date']})\n{a['abstract']}"
                for a in arts if a["pmid"])
            prompt = (
                "Voici les articles PubMed récents du fil de veille de l'équipe :\n\n"
                + abstracts[:12000] +
                "\n\nFais la synthèse de veille :\n1. LES 3–5 POINTS À RETENIR "
                "cette semaine (un par article clé)\n2. POUR CHAQUE POINT : ce que ça "
                "change concrètement pour un staff World Tour (protocole, cible, piège)\n"
                "3. SIGNAUX FAIBLES : sujets à surveiller prochainement"
            )
            results = multi_consult(models or [fallback_model], SYSTEM, prompt, key)
            for r in results:
                with st.expander(f"🤖 {r['model']}", expanded=r["ok"]):
                    st.markdown(r["content"] if r["ok"] else f"❌ {r['content']}")
            if any(r["ok"] for r in results) and models:
                with st.spinner("Synthèse croisée…"):
                    ok, content = synthesize(results, SYSTEM, prompt, key, models[0])
                st.markdown(content if ok else f"❌ {content}")
    st.subheader("💡 L'IA propose de nouveaux sujets")
    domain = st.text_area(
        "Ton domaine / tes priorités :",
        "Cyclisme sur route élite : contre-la-montre, course par étape, "
        "gestion de la fatigue, acclimatation chaleur/altitude, nutrition et "
        "récupération des coureurs World Tour.", key="watch_domain")
    if st.button("💡 Proposer 5 nouveaux sujets de veille", type="primary"):
        prompt = (
            f"Domaine de l'équipe : {domain}\n\nPropose 5 sujets de veille "
            "scientifique PRIORITAIRES (une ligne chacun : sujet + pourquoi maintenant "
            "+ requête PubMed anglaise prête à copier). Sois force de proposition : "
            "ancre-toi dans l'actualité de la recherche en physiologie du sport, "
            "sans inventer de données."
        )
        ok, content = chat(fallback_model, [{"role": "user", "content": prompt}],
                           key, max_tokens=1200)
        st.markdown(content if ok else f"❌ {content}")
