import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.vekta import VektaClient
from core.physiology import compute_pmc, readiness, metric_comments, vfc_verdict
from core.openrouter import chat, CURATED_MODELS, DEFAULT_MODEL
from core.config import openrouter_key

st.set_page_config(page_title="Fiche Coureur — WT Performance Hub", page_icon="🧑‍⚕️", layout="wide")
gate()

client = VektaClient()
riders = client.get_athletes()
if client.last_error:
    st.warning(f"Vekta : {client.last_error} — données de démonstration affichées.")
elif client.notice:
    st.info(client.notice)

manual_ids = st.session_state.setdefault("vekta_manual_ids", [])
with st.sidebar.expander("➕ Ajouter un coureur Vekta par son ID"):
    st.caption("Ouvre le profil du coureur dans l'app Vekta : son ID est le nombre dans l'URL.")
    mid = st.text_input("ID Vekta du coureur", key="vekta_mid")
    mname = st.text_input("Nom à afficher (si l'ID marche)", key="vekta_mname")
    if st.button("Ajouter ce coureur", key="vekta_madd"):
        mid_clean = mid.strip()
        if not mid_clean:
            st.error("Colle d'abord l'ID du coureur.")
        elif mid_clean in [m["id"] for m in manual_ids] or mid_clean in [a["id"] for a in riders]:
            st.warning("Ce coureur est déjà dans la liste.")
        else:
            try:
                client._get(f"/users/{mid_clean}")
                manual_ids.append({"id": mid_clean, "name": mname.strip()})
                st.success(f"ID {mid_clean} accepté par Vekta — coureur ajouté.")
                st.rerun()
            except PermissionError:
                st.error("Refusé (403) : ce coureur n'a pas d'abonnement Vekta actif, "
                         "ou il n'est pas dans tes workspaces.")
            except LookupError:
                st.error("Introuvable (404) : cet ID n'est pas un de tes athlètes.")
            except RuntimeError as e:
                st.error(str(e))
            except Exception as e:
                st.error("Vekta injoignable : " + str(e)[:150])
    if manual_ids and st.button("Retirer les coureurs ajoutés à la main", key="vekta_mclear"):
        st.session_state["vekta_manual_ids"] = []
        st.rerun()

with st.sidebar.expander("🔎 Diagnostic Vekta"):
    st.caption("Montre les espaces de travail vus par ta clé API, et ton rôle dans chacun.")
    if st.button("Voir mes espaces et mon rôle", key="vekta_diag"):
        try:
            me = client._get("/users/me")
            ws = me.get("workspaces") if isinstance(me, dict) else None
            if isinstance(ws, list) and ws:
                for w in ws:
                    if not isinstance(w, dict):
                        st.write(f"• {w}")
                        continue
                    nm = (w.get("name") or w.get("workspaceName") or w.get("title")
                          or w.get("id") or "?")
                    rl = (w.get("role") or w.get("workspaceRole")
                          or w.get("membershipRole") or "?")
                    st.write(f"• **{nm}** — rôle : {rl}")
            else:
                st.write("Aucun espace de travail visible depuis l'API.")
        except PermissionError:
            st.error("Vekta refuse (403) : ton compte n'a pas d'abonnement personnel actif — "
                     "l'API ne peut pas montrer tes espaces. Vérifie ton rôle dans l'app Vekta.")
        except Exception as e:
            st.error(str(e))
for mrec in manual_ids:
    if mrec["id"] not in [a["id"] for a in riders]:
        try:
            client._get(f"/users/{mrec['id']}")
            a = client.get_athlete(mrec["id"])
            if a.get("id") == mrec["id"]:
                if mrec["name"] and a.get("name") in (None, "", "?"):
                    a["name"] = mrec["name"]
                riders = riders + [a]
        except Exception:
            pass

labels = {a["id"]: f"{a.get('name','?')} — {a.get('role','')}" for a in riders}
rider_id = st.sidebar.selectbox("Coureur", list(labels.keys()), format_func=lambda x: labels[x],
                                key="fiche_rider")
st.session_state["rider_id"] = rider_id
athlete = client.get_athlete(rider_id)
m = client.get_metrics(rider_id)

st.title(f"🧑‍⚕️ Fiche Coureur — {athlete.get('name', rider_id)}")
st.caption(f"Profil : {athlete.get('role','n/c')} · {athlete.get('weight','?')} kg · FCmax {athlete.get('hrmax','?')} bpm")

score, comps = readiness(m)
tsb = float(m.get("ctl", 0)) - float(m.get("atl", 0))
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("❤️ VFC (matin)", f"{m.get('vfc_last','?')} ms", f"{m.get('vfc_trend_7d','?')} ms / 7j")
c2.metric("Baseline VFC", f"{m.get('vfc_baseline','?')} ms")
c3.metric("Découplage cardio", f"{m.get('decouplage_cardio','?')} %")
c4.metric("Sleep score", m.get("sleep_score", "?"))
c5.metric("Charge 7j", m.get("load_7d", "?"))
c6.metric("TSB (forme)", f"{tsb:.0f}", None if tsb == 0 else ("↗" if tsb > 0 else "↘"))
st.progress(score / 100.0, text=f"Readiness globale : {score}/100 · " +
            " · ".join(f"{k} {v}" for k, v in comps.items()))

st.subheader("Commentaires automatiques par métrique")
comments = metric_comments(m)
mc = st.columns(3)
for i, (k, v) in enumerate(comments.items()):
    with mc[i % 3]:
        st.markdown(f"**{k}**\n\n{v}")

left, right = st.columns(2)

with left:
    st.subheader("VFC — 30 jours")
    vfc_df = pd.DataFrame(m.get("vfc_series", []))
    if not vfc_df.empty:
        vfc_df["date"] = pd.to_datetime(vfc_df["date"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=vfc_df["date"], y=vfc_df["vfc"], name="VFC",
                                 line=dict(color="#e74c3c", width=2)))
        fig.add_hline(y=m.get("vfc_baseline", 0), line_dash="dash", line_color="#2980b9",
                      annotation_text="Baseline")
        fig.update_layout(height=320, template="plotly_white", margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)
        st.info(vfc_verdict(m))

    st.subheader("Charge d'entraînement (PMC)")
    load = client.get_daily_load(rider_id, 120)
    pmc = compute_pmc(load)
    if not pmc.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=pmc["Date"], y=pmc["Charge"], name="Charge quotidienne",
                             marker_color=np.where(pmc["Forme"] < 0, "#f1c40f", "#3498db"), opacity=0.5))
        fig.add_trace(go.Scatter(x=pmc["Date"], y=pmc["Fatigue"], name="Fatigue (ATL)",
                                 line=dict(color="#e74c3c", width=2)))
        fig.add_trace(go.Scatter(x=pmc["Date"], y=pmc["Fitness"], name="Fitness (CTL)",
                                 line=dict(color="#2980b9", width=3)))
        fig.update_layout(height=340, template="plotly_white", hovermode="x unified",
                          legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Profil de puissance (PPR)")
    pp = client.get_power_profile(rider_id)
    dur_map = [("p10s", 10), ("p20s", 20), ("p30s", 30), ("p1m", 60), ("p3m", 180),
               ("p5m", 300), ("p10m", 600), ("p20m", 1200)]
    xs, ys = [], []
    for key, sec in dur_map:
        if pp.get(key):
            xs.append(sec)
            ys.append(pp[key])
    if xs:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers+text", name="PPR",
                                 text=[f"{int(v)} W" for v in ys], textposition="top center",
                                 line=dict(color="#8e44ad", width=3)))
        fig.update_xaxes(type="log", title="Durée (s, échelle log)")
        fig.update_yaxes(title="Puissance (W)")
        fig.update_layout(height=320, template="plotly_white", margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(f"CP lissée : **{pp.get('cp','?')} W** ({pp.get('cp',0)/max(athlete.get('weight',1),1):.1f} W/kg)")

    st.subheader("🧠 Analyse IA complète (toutes les métriques)")
    model = st.selectbox("Modèle", CURATED_MODELS, index=CURATED_MODELS.index(DEFAULT_MODEL), key="fiche_model")
    if st.button("Générer l'analyse niveau staff World Tour", type="primary"):
        context = {
            "coureur": athlete,
            "metriques": m,
            "profil_puissance": pp,
            "tsb": round(tsb, 1),
            "readiness": {"score": score, "composantes": comps},
        }
        prompt = (
            "Tu es physiologiste d'équipe World Tour, docteur en physiologie de l'exercice. "
            "Voici les données complètes d'un coureur (JSON) :\n\n"
            + str(context) +
            "\n\nRédige en français une analyse structurée et opérationnelle :\n"
            "1. LECTURE PHYSIOLOGIQUE (VFC, découplage, sommeil, FC repos — croiser les signaux)\n"
            "2. ÉTAT DE FORME (CTL/ATL/TSB, ratio 7j/28j, fenêtre de performance)\n"
            "3. CAPACITÉS ACTUELLES (profil de puissance : forces/limites vs exigences de son profil de coureur)\n"
            "4. RISQUES (surentraînement, maladie, contre-performance)\n"
            "5. RECOMMANDATIONS 72 H (entraînement, récupération, nutrition)\n"
            "6. SI COURSE DEMAIN : rôle tactique recommandé et gestion d'effort.\n"
            "Sois précis, chiffré, sans généralités."
        )
        with st.spinner("Analyse en cours…"):
            ok, content = chat(model, [{"role": "user", "content": prompt}],
                               openrouter_key(), max_tokens=2500)
        if ok:
            st.markdown(content)
        else:
            st.error(f"OpenRouter : {content}")
