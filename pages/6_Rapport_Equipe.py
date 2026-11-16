import base64
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from core.auth import gate
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.physiology import readiness, metric_comments
from core.powermodel import fit_cp_model

st.set_page_config(page_title="Rapport Équipe — WT Performance Hub", page_icon="📋", layout="wide")
gate()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks"], key="report_src")
client = VektaClient()
if src == "TrainingPeaks":
    tp = session_client()
    if tp.configured:
        riders_tp = tp.get_athletes()
        if riders_tp:
            client = tp

riders = client.get_athletes()
if client.last_error:
    st.warning(f"Source : {client.last_error} — données de démonstration affichées.")

st.title("📋 Rapport d'équipe — hebdo staff")
st.caption("Une fiche par coureur : fraîcheur, charge, VFC et PPR du moment. "
           "Imprime en PDF (bouton dans le document) et envoie-le au staff.")

if not riders:
    st.info("Aucun coureur lisible dans la source choisie.")
    st.stop()

PERIODS = {"7 derniers jours": 7, "14 jours": 14, "21 jours (stage)": 21,
           "30 jours": 30, "90 jours": 90, "180 jours": 180,
           "Saison (365 j)": 365, "Choisir les dates": None}
per = st.selectbox("Période du rapport", list(PERIODS), key="rep_period")
if PERIODS[per] is None:
    dc1, dc2 = st.columns(2)
    d_from = dc1.date_input("Du", value=date.today() - pd.Timedelta(days=30), key="rep_from")
    d_to = dc2.date_input("Au", value=date.today(), key="rep_to")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    days = max(1, (d_to - d_from).days + 1)
    period_lbl = f"{d_from.strftime('%d/%m/%Y')} → {d_to.strftime('%d/%m/%Y')} ({days} j)"
    if days <= 1:
        period_lbl += " — rapport d'une séance"
else:
    days = PERIODS[per]
    period_lbl = f"{days} derniers jours"

names = [r["name"] for r in riders]
sel = st.multiselect("Coureurs du rapport", names, default=names[: min(8, len(names))])
rdate = st.date_input("Date du rapport", value=date.today())
logo_up = st.file_uploader("Logo (png/jpg)", type=["png", "jpg", "jpeg"], key="rep_logo")

DURS = [60, 180, 300, 1200, 3600]
DURS_LBL = ["1 min", "3 min", "5 min", "20 min", "60 min"]


def rider_row(r, days):
    aid = r["id"]
    m = client.get_metrics(aid, days=days)
    pp = client.get_power_profile(aid)
    efforts = (client.get_tt_efforts(aid, days=days)
               if hasattr(client, "get_tt_efforts") else [])
    pts = [(e["duration_s"], e["avg_w"]) for e in efforts
           if e.get("duration_s") and e.get("avg_w")]
    model = fit_cp_model(pts) if len(pts) >= 2 else None
    ppr = {}
    if model:
        for s in DURS:
            t = min(max(s, 20), 3600)
            ppr[s] = int(round(model["cp"] + model["wprime"] / t))
    else:
        pts = [(60, pp.get("p1m")), (180, pp.get("p3m")), (300, pp.get("p5m")),
               (600, pp.get("p10m")), (1200, pp.get("p20m"))]
        pts = [(s, v) for s, v in pts if v]
        for s in DURS:
            if pts:
                near = min(pts, key=lambda x: abs(np.log(x[0] / s)))
                ppr[s] = int(round(near[1]))
    score, _comps = readiness(m)
    comments = metric_comments(m)
    ctl = float(m.get("ctl", 0) or 0)
    atl = float(m.get("atl", 0) or 0)
    tsb = ctl - atl
    if tsb > 5:
        badge, bcol = "FRAIS", "#16a34a"
    elif tsb > -10:
        badge, bcol = "ÉQUILIBRE", "#d97706"
    else:
        badge, bcol = "FATIGUÉ", "#dc2626"
    hrv = m.get("vfc_last")
    hrv_base = m.get("vfc_baseline")
    return {"name": r["name"], "role": r.get("role", "—"),
            "weight": float(r.get("weight") or 0), "cp": int(pp.get("cp") or 0),
            "ctl": ctl, "atl": atl, "tsb": tsb,
            "load_7d": int(m.get("load_7d", 0) or 0),
            "score": score, "badge": badge, "bcol": bcol, "ppr": ppr,
            "comment": next(iter(comments.values()), "Données limitées."),
            "hrv": hrv, "hrv_base": hrv_base,
            "model_fit": bool(model)}


if st.button("📄 Générer le rapport", type="primary"):
    with st.spinner("Collecte des données coureurs…"):
        rows = [rider_row(riders[names.index(n)], days) for n in sel]

    logo_uri = ""
    if logo_up is not None:
        mime = "image/png" if (logo_up.type or "").endswith("png") else "image/jpeg"
        logo_uri = f"data:{mime};base64," + base64.b64encode(logo_up.getvalue()).decode()

    cards = ""
    for r in rows:
        ppr_cells = "".join(
            f'<td><b>{r["ppr"][s]}</b> W</td>' if s in r["ppr"] else "<td>—</td>"
            for s in DURS)
        hrv_txt = (f'{r["hrv"]:.0f} ms (base {r["hrv_base"]:.0f})'
                   if r["hrv"] and r["hrv_base"] else "non enregistrée")
        ppr_note = "modèle ajusté sur les efforts CLM" if r["model_fit"] else "profil puissance"
        cards += f'''
<div class="rider">
  <div class="rider-head">
    <div><div class="rname">{r["name"]}</div>
    <div class="rrole">{r["role"]} · {r["weight"]:.1f} kg · CP {r["cp"]} W</div></div>
    <div class="badge" style="background:{r["bcol"]}">{r["badge"]}</div>
  </div>
  <table class="grid"><tr>
    <td><span class="lbl">CTL</span><b>{r["ctl"]:.0f}</b></td>
    <td><span class="lbl">ATL</span><b>{r["atl"]:.0f}</b></td>
    <td><span class="lbl">Forme</span><b>{r["tsb"]:+.0f}</b></td>
    <td><span class="lbl">Charge 7j</span><b>{r["load_7d"]}</b></td>
    <td><span class="lbl">VFC</span><b>{hrv_txt}</b></td>
    <td><span class="lbl">Fraîcheur</span><b>{r["score"]}/100</b></td>
  </tr></table>
  <div class="ppr-title">PPR du moment ({ppr_note})</div>
  <table class="ppr"><tr>{"".join(f"<th>{l}</th>" for l in DURS_LBL)}</tr>
  <tr>{ppr_cells}</tr></table>
  <div class="comment">{r["comment"]}</div>
</div>'''

    logo_html = (f'<img src="{logo_uri}" style="max-height:46px;max-width:180px;object-fit:contain">'
                 if logo_uri else '<div style="font-weight:900;letter-spacing:3px;color:rgba(255,255,255,.5)">LOGO</div>')
    doc = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4 portrait; margin: 10mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #111318; background: #ffffff; }}
.hero {{ background: #111318; color: #fff; padding: 16px 20px; display: flex;
        align-items: center; gap: 16px; border-radius: 8px 8px 0 0; }}
.hero h1 {{ font-size: 22px; letter-spacing: 2px; text-transform: uppercase; }}
.hero .sub {{ font-size: 11px; color: rgba(255,255,255,.6); letter-spacing: 1px;
             text-transform: uppercase; }}
.rider {{ border: 1px solid #dde0e8; border-top: none; padding: 12px 16px; }}
.rider:first-of-type {{ border-top: 1px solid #dde0e8; }}
.rider-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.rname {{ font-size: 17px; font-weight: 900; text-transform: uppercase; letter-spacing: .5px; }}
.rrole {{ font-size: 11px; color: #7a8194; }}
.badge {{ color: #fff; font-size: 10px; font-weight: 700; letter-spacing: 1px;
         padding: 4px 10px; border-radius: 4px; }}
table.grid {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
table.grid td {{ background: #f4f5f7; border: 1px solid #dde0e8; padding: 6px 8px;
                text-align: center; }}
table.grid .lbl {{ display: block; font-size: 8px; letter-spacing: 1px;
                  text-transform: uppercase; color: #7a8194; margin-bottom: 2px; }}
table.grid b {{ font-size: 14px; }}
.ppr-title {{ font-size: 9px; letter-spacing: 1px; text-transform: uppercase;
             color: #7a8194; margin: 6px 0 4px; }}
table.ppr {{ width: 100%; border-collapse: collapse; }}
table.ppr th {{ background: #111318; color: #fff; font-size: 9px; padding: 4px;
               text-transform: uppercase; letter-spacing: 1px; }}
table.ppr td {{ border: 1px solid #dde0e8; padding: 5px 8px; text-align: center; font-size: 13px; }}
.comment {{ margin-top: 6px; font-size: 11px; color: #334155; background: #f8fafc;
           border-left: 3px solid #3b82f6; padding: 6px 10px; }}
.printbtn {{ position: fixed; top: 12px; right: 12px; background: #111318; color: #fff;
            border: none; padding: 10px 18px; border-radius: 6px; font-weight: bold;
            cursor: pointer; }}
@media print {{ .printbtn {{ display: none; }} }}
</style></head><body>
<button class="printbtn" onclick="window.print()">🖨️ Print / PDF</button>
<div class="hero">{logo_html}<div><div class="sub">Team report — {period_lbl}</div>
<h1>Rapport d'équipe</h1></div>
<div style="margin-left:auto;text-align:center;background:rgba(255,255,255,.1);padding:8px 16px;border-radius:6px">
<span style="font-size:10px;color:rgba(255,255,255,.6)">DATE</span><br>
<b style="font-size:16px">{rdate.strftime("%d/%m/%Y")}</b></div></div>
{cards}
</body></html>'''
    components.html(doc, height=1100, scrolling=True)

st.divider()
st.caption("Données : " + ("TrainingPeaks (lecture seule)" if isinstance(client, TPClient)
                           else "Vekta (API)") + ". Aucune donnée ne quitte l'application.")
