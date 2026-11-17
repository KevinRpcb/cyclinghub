# WT Performance Hub

Application Streamlit multi-pages pour le suivi d'une équipe cycliste :
fiche coureur (analyse IA complète), TT Center (PPR / SCx / cibles CLM),
prévision de performance GPX/FIT avec météo, échauffement WU24 natif,
et laboratoire scientifique multi-IA (OpenRouter + PubMed).

## Structure

```
app.py                    Accueil équipe : readiness, VFC, TSB, alertes
core/
  config.py               Lecture des Secrets (jamais de clé dans le code)
  auth.py                 Portail d'accès (hmac.compare_digest)
  vekta.py                Pont API Vekta + données de démonstration
  openrouter.py           OpenRouter : multi-modèles, synthèse, diagnostic
  pubmed.py               Recherche PubMed (Entrez)
  physiology.py           PMC, VFC, readiness, commentaires par métrique
  lactate.py              Seuils SV1/SV2, zones, verdicts tests lactate
  powermodel.py           Modèle CP, SCx, vitesses, puissance normalisée
  course.py               GPX/FIT, segments, météo Open-Meteo, pacing
pages/
  1_Fiche_Coureur.py      Page par coureur : métriques + analyse IA complète
  2_Plannif_Annuel.py     Calendrier par coureur, blocs, coach IA, sauvegarde chiffrée
  3_TT_Center.py          Suivi efforts CLM, PPR, estimation SCx, cible rapide
  4_Prevision_Perf.py     GPX/FIT → pacing + watts (type BestBikeSplit)
  5_Mode_TTT.py           Simulateur TTT, ordre de relais, analyse post-course
  6_Echauffement.py       WU24 natif : protocoles, custom, conditions, stickers
  7_Stem_Values.py        Constructeur de séances : watts par coureur, print à la carte, .fit
  8_Testing.py            Tests lactate (paliers, SV1 3×15'), test de terrain, collecte
  9_Labo.py               Veille PubMed multi-IA, brainstorming, propositions
  10_Rapport_Equipe.py    Rapport d'équipe : fiches, PPR, fatigue, PDF
```

## Secrets requis (Streamlit Cloud → App → Settings → Secrets)

Voir `.streamlit/secrets.toml.example` pour le modèle complet :

- `APP_PASSWORD` — mot de passe d'accès équipe (obligatoire).
- `OPENROUTER_API_KEY` — clé OpenRouter (pages coureur + labo).
- `VEKTA_API_KEY` — clé à générer dans Vekta → Settings → API keys
  (https://app.joinvekta.com/me/settings/api-keys). L'app interroge
  `https://api.joinvekta.com` (`/users`, métriques, sommeil, wellness,
  activités, best-efforts) ; `VEKTA_BASE_URL` est optionnel (défaut correct
  dans le code). Sans clé ou sans abonnement actif côté athlète, des données
  de démonstration sont affichées avec un avertissement.
- `PUBMED_EMAIL`, `PUBMED_API_KEY` (optionnel) — identifie les requêtes PubMed.
- `TP_USERNAME`, `TP_PASSWORD` (optionnel) — pont **TrainingPeaks** (lecture
  seule) via l'API interne du site web (celle qu'utilise WKO5). Ajoute une
  source « TrainingPeaks » dans les pages Fiche Coureur, TT Center,
  Prévision Perf et Échauffement : athlètes coachés, CTL/ATL/TSB, charge TSS,
  séances, efforts vélo et meilleurs pics puissance (CP estimée). API non
  officielle : peut changer sans préavis ; l'app retombe sur Vekta en cas
  d'échec.
- `[WU_PROTOCOLS.*]` — **les détails chiffrés des protocoles d'échauffement
  restent privés** : le code ne contient que les titres. Colle la table
  `[WU_PROTOCOLS]` dans les Secrets (modèle de champs dans l'exemple) ;
  sans cette table, la page Échauffement affiche les instructions.

## Sécurité

- Aucune clé en dur : tout passe par `st.secrets`.
- Comparaison de mot de passe à temps constant (`hmac.compare_digest`).
- Timeouts sur tous les appels HTTP (OpenRouter, Vekta, Open-Meteo, PubMed).
- Uploads validés (extension, colonnes requises) avant traitement.
- Aucune donnée coureur n'est envoyée à un tiers sans action explicite.

## Lancement local

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml  # puis remplir
streamlit run app.py
```
