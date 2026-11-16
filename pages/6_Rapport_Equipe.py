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

st.title("📋 Rapport d'équipe — staff")
st.caption("Une fiche par coureur : volume, comparaisons saison/saison -1/all-time, "
           "PPR, fatigue. Imprime en PDF (bouton dans le document) et envoie-le au staff.")

if not riders:
    st.info("Aucun coureur lisible dans la source choisie.")
    st.stop()

PERIODS = {"7 derniers jours": 7, "14 jours": 14, "21 jours (stage)": 21,
           "30 jours": 30, "90 jours": 90, "180 jours": 180,
           "Saison (365 j)": 365, "Choisir les dates": None}
per = st.selectbox("Période du rapport", list(PERIODS), key="rep_period")
offset = 0
if PERIODS[per] is None:
    dc1, dc2 = st.columns(2)
    d_from = dc1.date_input("Du", value=date.today() - pd.Timedelta(days=30), key="rep_from")
    d_to = dc2.date_input("Au", value=date.today(), key="rep_to")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    days = max(1, (d_to - d_from).days + 1)
    offset = max(0, (date.today() - d_to).days)
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


def _ppr_from_efforts(efforts):
    pts = [(e["duration_s"], e["avg_w"]) for e in efforts
           if e.get("duration_s") and e.get("avg_w")]
    model = fit_cp_model(pts) if len(pts) >= 2 else None
    if not model:
        return None
    return {s: int(round(model["cp"] + model["wprime"] / min(max(s, 20), 3600)))
            for s in DURS}


def _ppr_from_profile(pp):
    if not pp:
        return None
    out = {}
    for s in DURS:
        if s == 60:
            v = pp.get("p1m")
        elif s == 180:
            v = pp.get("p3m")
        elif s == 300:
            v = pp.get("p5m")
        elif s == 1200:
            v = pp.get("p20m")
        else:
            v = pp.get("p60m") or pp.get("cp")
        if v:
            out[s] = int(round(v))
    return out or None


def rider_row(r, days, offset=0):
    aid = r["id"]
    errs = []

    def safe(label, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TypeError:
            try:
                return fn(*args)
            except Exception as e2:
                errs.append(f"{label} : {type(e2).__name__} — {e2}")
                return None
        except Exception as e:
            errs.append(f"{label} : {type(e).__name__} — {e}")
            return None

    m = safe("métriques", client.get_metrics, aid, days=days) or {}
    pp = safe("profil puissance", client.get_power_profile, aid) or {}
    efforts = []
    if hasattr(client, "get_tt_efforts"):
        efforts = safe("efforts CLM", client.get_tt_efforts,
                       aid, days=days, offset_days=offset) or []
    ppr = _ppr_from_efforts(efforts)
    model_fit = bool(ppr)
    if not ppr:
        pts = [(60, pp.get("p1m")), (180, pp.get("p3m")), (300, pp.get("p5m")),
               (600, pp.get("p10m")), (1200, pp.get("p20m"))]
        pts = [(s, v) for s, v in pts if v]
        ppr = {}
        for s in DURS:
            if pts:
                near = min(pts, key=lambda x: abs(np.log(x[0] / s)))
                ppr[s] = int(round(near[1]))

    ppr_prev = None
    n_eff_prev = 0
    if hasattr(client, "get_tt_efforts"):
        eff_prev = safe("efforts saison -1", client.get_tt_efforts,
                        aid, days=365, offset_days=365 + offset) or []
        n_eff_prev = len(eff_prev)
        ppr_prev = _ppr_from_efforts(eff_prev)
    ppr_all = _ppr_from_profile(pp)

    def vol(dd, oo=0):
        if hasattr(client, "get_volume"):
            return safe(f"volume {dd} j", client.get_volume,
                        aid, days=dd, offset_days=oo) or {}
        return {}

    vol_cur = vol(days, offset)
    vol_prev = vol(days, days + offset)
    vol_season = vol(365)
    vol_prev_season = vol(365, 365)

    score, _comps = readiness(m)
    comments = metric_comments(m)
    ctl = float(m.get("ctl", 0) or 0)
    atl = float(m.get("atl", 0) or 0)
    tsb = ctl - atl
    load_7d = int(m.get("load_7d", 0) or 0)
    load_28d = int(m.get("load_28d", 0) or 0)
    acwr = round(load_7d / (load_28d / 4.0), 2) if load_28d else None
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
            "ctl": ctl, "atl": atl, "tsb": tsb, "acwr": acwr,
            "load_7d": load_7d, "load_28d": load_28d,
            "score": score, "badge": badge, "bcol": bcol, "ppr": ppr,
            "ppr_prev": ppr_prev, "ppr_all": ppr_all,
            "comment": next(iter(comments.values()), "Données limitées."),
            "hrv": hrv, "hrv_base": hrv_base,
            "sleep": m.get("sleep_score"),
            "vol": vol_cur, "vol_prev": vol_prev,
            "vol_season": vol_season, "vol_prev_season": vol_prev_season,
            "model_fit": model_fit, "errors": errs,
            "n_eff_now": len(efforts), "n_eff_prev": n_eff_prev}


def _delta(cur, prev):
    if not cur or not prev:
        return ""
    pct = (cur - prev) / prev * 100.0
    col = "#16a34a" if pct >= 0 else "#dc2626"
    return f'<span class="dlt" style="color:{col}">{pct:+.0f} %</span>'


def _fmt_km(v):
    return f"{v:,.0f}".replace(",", " ") if v else "—"


if st.button("📄 Générer le rapport", type="primary"):
    with st.spinner("Collecte des données coureurs…"):
        rows = [rider_row(riders[names.index(n)], days, offset) for n in sel]
    all_errs = [e for r in rows for e in r["errors"]]
    if all_errs:
        st.warning("Certaines données n'ont pas pu être lues — la fiche reste générée : "
                   + " | ".join(sorted(set(all_errs))))

    logo_uri = ""
    if logo_up is not None:
        mime = "image/png" if (logo_up.type or "").endswith("png") else "image/jpeg"
        logo_uri = f"data:{mime};base64," + base64.b64encode(logo_up.getvalue()).decode()

    cards = ""
    for r in rows:
        v, vp = r["vol"], r["vol_prev"]
        vs, vps = r["vol_season"], r["vol_prev_season"]
        vol_cells = "".join(
            f'<td><span class="lbl">{lbl}</span><b>{val}</b> {dlt}</td>'
            for lbl, val, dlt in [
                ("Séances", v.get("sessions", "—") if v else "—",
                 _delta(v.get("sessions"), vp.get("sessions")) if v and vp else ""),
                ("Heures", f'{v["hours"]:,}'.replace(",", " ") if v and v.get("hours") else "—",
                 _delta(v.get("hours"), vp.get("hours")) if v and vp else ""),
                ("Km", _fmt_km(v.get("km")) if v else "—",
                 _delta(v.get("km"), vp.get("km")) if v and vp else ""),
                ("KJ", _fmt_km(v.get("kj")) if v else "—",
                 _delta(v.get("kj"), vp.get("kj")) if v and vp else ""),
                ("Charge", _fmt_km(v.get("tss")) if v and v.get("tss") else f'{r["load_7d"]}',
                 _delta(v.get("tss"), vp.get("tss")) if v and vp and v.get("tss") else ""),
            ])
        season_cells = "".join(
            f'<td><span class="lbl">{lbl}</span><b>{val}</b> {dlt}</td>'
            for lbl, val, dlt in [
                ("Km saison", _fmt_km(vs.get("km")) if vs else "—",
                 _delta(vs.get("km"), vps.get("km")) if vs and vps else ""),
                ("Heures", f'{vs["hours"]:,}'.replace(",", " ") if vs and vs.get("hours") else "—",
                 _delta(vs.get("hours"), vps.get("hours")) if vs and vps else ""),
                ("Séances", vs.get("sessions", "—") if vs else "—",
                 _delta(vs.get("sessions"), vps.get("sessions")) if vs and vps else ""),
            ])
        hrv_txt = (f'{r["hrv"]:.0f} ms (base {r["hrv_base"]:.0f})'
                   if r["hrv"] and r["hrv_base"] else "non enregistrée")
        sleep_txt = f'{r["sleep"]}/100' if r.get("sleep") else "—"
        acwr_txt = f'{r["acwr"]:.2f}' if r.get("acwr") else "—"
        fatigue_cells = "".join(
            f'<td><span class="lbl">{lbl}</span><b>{val}</b></td>' for lbl, val in [
                ("CTL", f'{r["ctl"]:.0f}'), ("ATL", f'{r["atl"]:.0f}'),
                ("Forme", f'{r["tsb"]:+.0f}'), ("Charge 7j", f'{r["load_7d"]}'),
                ("ACWR", acwr_txt), ("VFC", hrv_txt), ("Sommeil", sleep_txt),
                ("Fraîcheur", f'{r["score"]}/100'),
            ])
        ppr_rows = ""
        for i, s in enumerate(DURS):
            cur = f'{r["ppr"][s]} W' if s in r["ppr"] else "—"
            prv = f'{r["ppr_prev"][s]}' if r["ppr_prev"] and s in r["ppr_prev"] else "—"
            allw = f'{r["ppr_all"][s]}' if r["ppr_all"] and s in r["ppr_all"] else "—"
            cls = ' class="alt"' if i % 2 else ""
            ppr_rows += (f'<tr{cls}><td class="dur">{DURS_LBL[i]}</td>'
                         f'<td><b>{cur}</b></td><td>{prv}</td><td>{allw}</td></tr>')
        ppr_note = ("modèle ajusté sur les efforts de la période" if r["model_fit"]
                    else "profil puissance (all-time)")
        miss_prev = ("" if r["ppr_prev"]
                     else " — aucun effort CLM trouvé l'an dernier sur cette fenêtre")
        cards += f'''
<div class="rider">
  <div class="rider-head">
    <div><div class="rname">{r["name"]}</div>
    <div class="rrole">{r["role"]} · {r["weight"]:.1f} kg · CP {r["cp"]} W</div></div>
    <div class="badge" style="background:{r["bcol"]}">{r["badge"]}</div>
  </div>
  <div class="sec">Volume — période ({period_lbl}) · <span class="dlt2">écart vs période précédente</span></div>
  <table class="grid"><tr>{vol_cells}</tr></table>
  <div class="sec">Saison (365 j) · <span class="dlt2">écart vs saison précédente</span></div>
  <table class="grid"><tr>{season_cells}</tr></table>
  <div class="sec">Fatigue & fraîcheur</div>
  <table class="grid"><tr>{fatigue_cells}</tr></table>
  <div class="ppr-title">PPR — puissance moyenne maximale ({ppr_note})</div>
  <table class="ppr"><tr><th>Durée</th><th>Période</th><th>Saison -1</th><th>All-time</th></tr>
  {ppr_rows}</table>
  <div class="ppr-miss">Efforts trouvés — période : {r["n_eff_now"]} · saison -1 : {r["n_eff_prev"]}{miss_prev}</div>
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
.sec {{ font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: #334155;
       font-weight: 700; margin: 8px 0 4px; }}
.sec .dlt2 {{ color: #7a8194; font-weight: 400; letter-spacing: 0; text-transform: none; }}
table.grid {{ width: 100%; border-collapse: collapse; }}
table.grid td {{ background: #f4f5f7; border: 1px solid #dde0e8; padding: 6px 8px;
                text-align: center; }}
table.grid .lbl {{ display: block; font-size: 8px; letter-spacing: 1px;
                  text-transform: uppercase; color: #7a8194; margin-bottom: 2px; }}
table.grid b {{ font-size: 14px; }}
.dlt {{ font-size: 9px; font-weight: 700; margin-left: 3px; }}
.ppr-title {{ font-size: 9px; letter-spacing: 1px; text-transform: uppercase;
             color: #7a8194; margin: 6px 0 4px; }}
table.ppr {{ width: 100%; border-collapse: collapse; }}
table.ppr th {{ background: #111318; color: #fff; font-size: 9px; padding: 4px;
               text-transform: uppercase; letter-spacing: 1px; }}
table.ppr td {{ border: 1px solid #dde0e8; padding: 5px 8px; text-align: center; font-size: 13px; }}
table.ppr tr.alt td {{ background: #f8fafc; }}
table.ppr td.dur {{ font-weight: 700; background: #f4f5f7; font-size: 11px; }}
.ppr-miss {{ font-size: 9px; color: #7a8194; margin-top: 3px; }}
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
    with st.expander("🔍 Diagnostic données du rapport"):
        for r in rows:
            st.write(f"**{r['name']}** : efforts période **{r['n_eff_now']}** · "
                     f"saison -1 **{r['n_eff_prev']}** · PPR période "
                     f"{'✅' if r['ppr'] else '—'} · all-time "
                     f"{'✅' if r['ppr_all'] else '—'} · volume "
                     f"{'✅' if r['vol'] else '—'}")
        if getattr(client, "last_error", None):
            st.write(f"Note source : {client.last_error}")
        st.caption("Photographie ce bloc et envoie-le moi si des colonnes restent vides.")
    components.html(doc, height=1100, scrolling=True)

st.divider()
st.caption("Données : " + ("TrainingPeaks (lecture seule)" if isinstance(client, TPClient)
                           else "Vekta (API)") + ". Aucune donnée ne quitte l'application.")
