import os
import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime
from Bio import Entrez
import streamlit.components.v1 as components

# --- CONFIGURATION INITIALE ---
st.set_page_config(page_title="WT Performance Hub", layout="wide", initial_sidebar_state="expanded")

# Récupération sécurisée des clés via les Secrets Streamlit Cloud
OPENROUTER_API_KEY = st.secrets.get("OPENROUTER_API_KEY", "")
VEKTA_API_KEY = st.secrets.get("VEKTA_API_KEY", "")
Entrez.email = "coach.wt@equipe.com"

# ==========================================
# 1. MOTEURS DE DONNÉES & IA
# ==========================================

class ConnecteurVekta:
    """Connecteur API Vekta"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.vekta.com/v1"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.cp_actuel = 415 # CP lissé 120 jours

    def get_metriques_recentes(self):
        # Simulation en attendant le mapping direct de tes endpoints Vekta
        return {
            "vfc_matin": 42,
            "baseline_vfc": 65,
            "decouplage_cardio": 2.1,
            "charge_7j": 850
        }

def generer_donnees_pmc():
    """Génère l'historique de charge pour le graphique PMC"""
    dates = pd.date_range(end=datetime.today(), periods=90)
    charge = np.random.normal(60, 30, len(dates))
    charge = np.where(charge < 0, 0, charge)
    df = pd.DataFrame({'Date': dates, 'Charge': charge})
    df['Fitness'] = df['Charge'].ewm(span=42, adjust=False).mean()
    df['Fatigue'] = df['Charge'].ewm(span=7, adjust=False).mean()
    df['Forme'] = df['Fitness'].shift(1) - df['Fatigue'].shift(1)
    return df.fillna(0)

def analyser_vfc_dynamique(metriques):
    """Juge de paix : Croisement VFC / Efficacité mécanique"""
    vfc = metriques["vfc_matin"]
    base = metriques["baseline_vfc"]
    decouplage = metriques["decouplage_cardio"]
    if vfc < (base * 0.85):
        if decouplage < 4.0:
            return f"⚠️ VFC basse ({vfc} ms) MAIS mécanique excellente (Dérive : {decouplage}%).\n\n**Analyse :** Hyper-activation sympathique (Mode Course). Feu vert tactique."
        else:
            return f"🚨 VFC basse ({vfc} ms) ET perte d'efficacité (Dérive : {decouplage}%).\n\n**Analyse :** Fatigue systémique. Bloc d'assimilation urgent."
    return f"✅ VFC dans la norme ({vfc} ms). Assimilation validée."

def appeler_openrouter(prompt, modele="anthropic/claude-3.5-sonnet"):
    if not OPENROUTER_API_KEY:
        return "Clé OpenRouter non configurée dans les Secrets Streamlit."
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {"model": modele, "messages": [{"role": "user", "content": prompt}]}
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data).json()
        return res['choices'][0]['message']['content']
    except Exception as e:
        return f"Erreur API : {e}"

def chercher_pubmed(requete):
    try:
        handle = Entrez.esearch(db="pubmed", term=requete, retmax=10)
        id_list = Entrez.read(handle)["IdList"]
        if not id_list:
            return "Aucun article trouvé."
        fetch = Entrez.efetch(db="pubmed", id=id_list, retmode="xml")
        records = Entrez.read(fetch)
        abstracts = []
        for article in records['PubmedArticle']:
            titre = article['MedlineCitation']['Article']['ArticleTitle']
            try:
                txt = article['MedlineCitation']['Article']['Abstract']['AbstractText'][0]
                abstracts.append(f"Titre : {titre}\nRésumé : {txt}")
            except:
                continue
        return "\n\n".join(abstracts)
    except:
        return "Erreur lors de la récupération PubMed."

# ==========================================
# 2. PROTOCOLES D'ÉCHAUFFEMENT CLM (WU24)
# ==========================================
protocoles_clm = {
    "standard": {"nom": "Standard (23')", "z3_blocks": 3, "z3_pct": 0.94, "z4_pct": [1.07, 1.12], "z5_pct": 1.24},
    "prologo": {"nom": "Prologue / ITT (25')", "z3_blocks": 2.5, "z3_pct": 0.94, "z4_pct": [1.07, 1.12], "z5_pct": 1.35},
    "ttt": {"nom": "TTT (23')", "z3_blocks": 3, "z3_pct": 0.94, "z4_pct": [1.07, 1.12], "z5_pct": 1.35},
    "hot": {"nom": "Départ Nerveux (18')", "z3_blocks": 2, "z3_pct": 0.94, "z4_pct": [1.07, 1.12], "z5_pct": 1.24},
    "quiet": {"nom": "Départ Contrôlé (20')", "z3_blocks": 3, "z3_pct": 0.94, "z4_pct": [0.99, 1.05], "z5_pct": 1.35},
    "gt": {"nom": "Grand Tour Fatigue (18')", "z3_blocks": 3, "z3_pct": 0.94, "z4_pct": [1.01, 1.12], "z5_pct": 1.24}
}

# ==========================================
# 3. INTERFACE UTILISATEUR
# ==========================================

# --- SIDEBAR ---
st.sidebar.header("⚙️ Configuration")
athlete = st.sidebar.selectbox("Coureur", ["Coureur A (Grimpeur)", "Coureur B (Rouleur)", "Coureur C (Sprinteur)"])
systeme = st.sidebar.radio("Système de Métriques", ["Vekta (Défaut)", "WKO5 (TSS/CTL)"])

if systeme == "WKO5 (TSS/CTL)":
    vocab = {"charge": "TSS", "fitness": "CTL", "fatigue": "ATL", "forme": "TSB"}
else:
    vocab = {"charge": "Vekta Load", "fitness": "Vekta Fitness", "fatigue": "Vekta Fatigue", "forme": "Vekta Form"}

# --- ONGLETS PRINCIPAUX ---
tab_bord, tab_clm, tab_labo = st.tabs(["📊 Dashboard & Tactique", "⏱️ TT Center", "🔬 Veille Scientifique"])

# ---------------------------------------------------------
# ONGLET 1 : TABLEAU DE BORD & ANALYSE DYNAMIQUE
# ---------------------------------------------------------
with tab_bord:
    api = ConnecteurVekta(VEKTA_API_KEY)
    metriques = api.get_metriques_recentes()
   
    st.subheader(f"Statistiques & Analyse — {athlete}")
   
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(f"Charge 7J ({vocab['charge']})", metriques['charge_7j'])
    col2.metric("Découplage Mécanique", f"{metriques['decouplage_cardio']} %", "-0.2%", delta_color="inverse")
   
    with col3:
        with st.popover(f"❤️ VFC : {metriques['vfc_matin']} ms"):
            st.markdown(analyser_vfc_dynamique(metriques))
           
    with col4:
        with st.popover("🧠 Brainstorming Tactique (IA)"):
            sujet = st.text_area("Problématique :", "Propose 3 séances spécifiques pour corriger le découplage mécanique observé.")
            if st.button("Consulter Claude 3.5"):
                st.write(appeler_openrouter(sujet, "anthropic/claude-3.5-sonnet"))

    # Graphique PMC
    df = generer_donnees_pmc()
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df['Date'], y=df['Forme'], name=vocab['forme'], marker_color=np.where(df['Forme']<0, '#f1c40f', '#3498db'), opacity=0.6))
    fig.add_trace(go.Scatter(x=df['Date'], y=df['Fatigue'], name=vocab['fatigue'], line=dict(color='#e74c3c', width=2)))
    fig.add_trace(go.Scatter(x=df['Date'], y=df['Fitness'], name=vocab['fitness'], line=dict(color='#2980b9', width=4)))
    fig.update_layout(height=420, template="plotly_white", hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------
# ONGLET 2 : TT CENTER (ÉCHAUFFEMENTS & STICKERS)
# ---------------------------------------------------------
with tab_clm:
    st.subheader("Planificateur d'Échauffement (Profil CP)")
   
    col_p, col_c = st.columns(2)
    choix_proto = col_p.selectbox("Sélectionner le Protocole", list(protocoles_clm.keys()), format_func=lambda x: protocoles_clm[x]['nom'])
    cp_saisi = col_c.number_input("Critical Power (CP) lissé du coureur (Watts)", value=api.cp_actuel)
   
    proto = protocoles_clm[choix_proto]
   
    st.markdown("### 📋 Feuille de route :")
    st.info(f"**Zone 3 (Activation) :** {proto['z3_blocks']} blocs ciblant **{int(cp_saisi * proto['z3_pct'])} W**")
    st.warning(f"**Zone 4 (Seuil) :** Progression de **{int(cp_saisi * proto['z4_pct'][0])} W** à **{int(cp_saisi * proto['z4_pct'][1])} W**")
    st.error(f"**Zone 5 (Déblocage PMA) :** Pics à **{int(cp_saisi * proto['z5_pct'])} W**")
   
    components.html("""
        <button onclick="window.print()" style="background:#111318;color:white;padding:10px 18px;border-radius:6px;border:none;cursor:pointer;font-weight:bold;margin-top:10px;">
        🖨️ Imprimer Planche Stickers Guidon (4×7 cm)
        </button>
    """, height=55)

# ---------------------------------------------------------
# ONGLET 3 : VEILLE SCIENTIFIQUE (PUBMED & IA)
# ---------------------------------------------------------
with tab_labo:
    st.subheader("Analyse de la Littérature Scientifique (PubMed)")
    sujet_recherche = st.text_input("Mots-clés en anglais :", "Heat acclimation cycling time trial")
   
    if st.button("Lancer l'analyse critique"):
        with st.spinner("Recherche PubMed en cours..."):
            abstracts = chercher_pubmed(sujet_recherche)
       
        if "Erreur" not in abstracts and "Aucun article" not in abstracts:
            with st.spinner("Analyse par Claude 3.5 en cours..."):
                prompt = f"""Tu es un physiologiste WorldTour. Analyse ces abstracts :\n\n{abstracts}\n\n
                Structure ta réponse :
                1. CONSENSUS ACTUEL : Que disent les études ?
                2. CRITIQUE MÉTHODOLOGIQUE : Failles pour des athlètes élites WorldTour.
                3. FORCE DES CONCLUSIONS : Recommandation pratique terrain."""
                analyse = appeler_openrouter(prompt, "anthropic/claude-3.5-sonnet")
                st.success("Analyse terminée")
                st.markdown(analyse)
                with st.expander("Voir les abstracts bruts"):
                    st.text(abstracts)
        else:
            st.error(abstracts)
