import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.physiology import compute_pmc, readiness, metric_comments, vfc_verdict
from core.powermodel import robust_cp_model
from core.openrouter import chat, CURATED_MODELS, DEFAULT_MODEL
from core.config import openrouter_key

st.set_page_config(page_title="Fiche Coureur — WT Performance Hub", page_icon="🧑‍⚕️", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks"], key="fiche_src")
client = VektaClient()
if src == "TrainingPeaks":
    tp = session_client()
    if not tp.configured:
        st.warning("TrainingPeaks : ajoute TP_USERNAME et TP_PASSWORD dans les Secrets "
                   "Streamlit (identifiants de ton compte coach TP) — Vekta est utilisée "
                   "en attendant.")
    else:
        riders_tp = tp.get_athletes()
        if riders_tp:
            client = tp
        else:
            st.warning(f"TrainingPeaks : {tp.last_error} — repli sur Vekta.")
riders = client.get_athletes()
if client.last_error:
    st.warning(f"Vekta : {client.last_error} — données de démonstration affichées.")
elif client.notice:
    st.info(client.notice)

if isinstance(client, VektaClient):
    manual_ids = st.session_state.setdefault("vekta_manual_ids", [])
    with st.sidebar.expander("➕ Ajouter un coureur Vekta par son ID"):
        st.caption("Ouvre le profil du coureur dans l'app Vekta : copie l'identifiant complet depuis "
                   "l'URL (ex. usr_abc123), avec le préfixe usr_ et tout ce qui suit.")
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
_keys = list(labels.keys())
_shared = st.session_state.get("rider_id")
_idx = _keys.index(_shared) if _shared in _keys else 0
rider_id = st.sidebar.selectbox("Coureur", _keys, index=_idx, format_func=lambda x: labels[x],
                                key="fiche_rider")
st.session_state["rider_id"] = rider_id
athlete = client.get_athlete(rider_id)
m = client.get_metrics(rider_id)

st.caption("Source des données : " + ("TrainingPeaks (lecture seule)"
            if isinstance(client, TPClient) else "Vekta (API)"))

if isinstance(client, TPClient):
    with st.expander("🗓️ Séances TrainingPeaks — 14 derniers jours"):
        _s = (pd.Timestamp.today().normalize() - pd.Timedelta(days=13)).strftime("%Y-%m-%d")
        _e = pd.Timestamp.today().strftime("%Y-%m-%d")
        wos = client.get_workouts(rider_id, _s, _e)
        if wos:
            wdf = pd.DataFrame(wos)
            cols = [c for c in ["date", "title", "duration_h", "distance_km",
                                "tss", "avg_w", "avg_hr"] if c in wdf.columns]
            wdf = wdf[cols].sort_values("date", ascending=False)
            wdf.columns = [c.replace("_", " ") for c in wdf.columns]
            st.dataframe(wdf, hide_index=True)
        else:
            extra = f" ({client.last_error})" if client.last_error else ""
            st.caption("Aucune séance lisible dans TrainingPeaks sur les 14 derniers jours." + extra)

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
    if not xs:
        pp_eff = (client.get_tt_efforts(rider_id)
                  if hasattr(client, "get_tt_efforts") else [])
        pts = [(e["duration_s"], e["avg_w"]) for e in pp_eff
               if e.get("duration_s") and e.get("avg_w")]
        mdl = robust_cp_model(pts, pp)
        if mdl:
            xs = [p[0] for p in mdl["points"]]
            ys = [p[1] for p in mdl["points"]]
    if xs:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers+text", name="PPR",
                                 text=[f"{int(v)} W" for v in ys], textposition="top center",
                                 line=dict(color="#8e44ad", width=3)))
        fig.update_xaxes(type="log", title="Durée (s, échelle log)")
        fig.update_yaxes(title="Puissance (W)")
        fig.update_layout(height=320, template="plotly_white", margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)
        if pp.get("cp"):
            st.caption(f"CP lissée : **{pp.get('cp','?')} W** "
                       f"({pp.get('cp',0)/max(athlete.get('weight',1),1):.1f} W/kg)")
    else:
        st.info("Profil de puissance indisponible pour ce coureur — ajoute des efforts "
                "avec puissance (CLM) ou vérifie la source de données.")

    st.subheader("🧠 Analyse IA complète (toutes les métriques)")
    model = st.selectbox("Modèle", CURATED_MODELS, index=CURATED_MODELS.index(DEFAULT_MODEL), key="fiche_model")
    if st.button("Générer l'analyse", type="primary"):
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
            st.session_state["fiche_analysis"] = content
            st.markdown(content)
        else:
            st.error(f"OpenRouter : {content}")
    elif st.session_state.get("fiche_analysis"):
        with st.expander("\U0001f9e0 Analyse IA g\u00e9n\u00e9r\u00e9e pr\u00e9c\u00e9demment"):
            st.markdown(st.session_state["fiche_analysis"])

    if st.button("\U0001f5a8\ufe0f Fiche imprimable (PDF)"):
        st.session_state["fiche_print"] = True

# ── Fiche imprimable ─────────────────────────────────────────────
if st.session_state.get("fiche_print"):
    def _fmt(v, suffix=""):
        return f"{v}{suffix}" if v not in (None, "", "?") else "\u2014"

    def _row(cells):
        return "".join(f'<td><span class="lbl">{k}</span><b>{v}</b></td>'
                       for k, v in cells)

    an = st.session_state.get("fiche_analysis") or ""
    an_html = ("".join(f'<p>{ln}</p>' for ln in an.splitlines() if ln.strip())
               if an else '<p class="mut">Analyse IA non g\u00e9n\u00e9r\u00e9e.</p>')
    ppr_cells = ""
    for key, lbl in [("p10s", "10 s"), ("p1m", "1 min"), ("p5m", "5 min"),
                     ("p20m", "20 min"), ("cp", "CP")]:
        if pp.get(key):
            wkg = pp[key] / max(float(athlete.get("weight") or 1), 1)
            ppr_cells += (f'<td><span class="lbl">{lbl}</span>'
                          f'<b>{pp[key]:.0f} W</b> <span class="mut">{wkg:.1f}/kg</span></td>')
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4 portrait; margin: 10mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #111318; background: #fff; }}
.hero {{ background: #111318; color: #fff; padding: 16px 20px; display: flex;
        justify-content: space-between; align-items: center; border-radius: 8px 8px 0 0; }}
.hero h1 {{ font-size: 20px; letter-spacing: 2px; text-transform: uppercase; }}
.hero .sub {{ font-size: 11px; color: rgba(255,255,255,.6); text-transform: uppercase;
             letter-spacing: 1px; }}
table.grid {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
table.grid td {{ background: #f4f5f7; border: 1px solid #dde0e8; padding: 7px 9px;
                text-align: center; }}
table.grid .lbl {{ display: block; font-size: 8px; letter-spacing: 1px;
                  text-transform: uppercase; color: #7a8194; margin-bottom: 2px; }}
table.grid b {{ font-size: 14px; }}
.mut {{ color: #7a8194; font-size: 10px; }}
.sec {{ font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: #334155;
       font-weight: 700; margin: 14px 0 2px; }}
.comment {{ font-size: 11px; color: #334155; background: #f8fafc;
           border-left: 3px solid #3b82f6; padding: 6px 10px; }}
.analysis {{ font-size: 12px; line-height: 1.55; margin-top: 6px; }}
.analysis p {{ margin: 5px 0; }}
.printbtn {{ position: fixed; top: 12px; right: 12px; background: #111318; color: #fff;
            border: none; padding: 10px 18px; border-radius: 6px; font-weight: 700;
            cursor: pointer; }}
@media print {{ .printbtn {{ display: none; }} }}
</style></head><body>
<button class="printbtn" onclick="window.print()">\U0001f5a8\ufe0f Imprimer / PDF</button>
<div class="hero">
  <div><h1>{athlete.get('name', rider_id)}</h1>
  <div class="sub">{athlete.get('role', 'n/c')} \u00b7 WT Performance Hub</div></div>
  <div class="sub">Fiche coureur \u2014 {pd.Timestamp.today().strftime('%d/%m/%Y')}</div>
</div>
<div class="sec">État du jour</div>
<table class="grid"><tr>{_row([("VFC", _fmt(m.get('vfc_last'), ' ms')),
    ("Baseline", _fmt(m.get('vfc_baseline'), ' ms')),
    ("D\u00e9couplage", _fmt(m.get('decouplage_cardio'), ' %')),
    ("Sommeil", _fmt(m.get('sleep_score')))])}</tr><tr>{_row([
    ("Charge 7j", _fmt(m.get('load_7d'))),
    ("CTL", f"{float(m.get('ctl', 0)):.0f}"),
    ("ATL", f"{float(m.get('atl', 0)):.0f}"),
    ("Forme (TSB)", f"{tsb:+.0f}"),
    ("Readiness", f"{score}/100")])}</tr></table>
<div class="sec">Profil de puissance (all-time)</div>
<table class="grid"><tr>{ppr_cells or '<td class="mut">Aucune donn\u00e9e puissance</td>'}</tr></table>
<div class="sec">Analyse staff</div>
<div class="analysis">{an_html}</div>
</body></html>"""
    components.html(doc, height=1200, scrolling=True)
