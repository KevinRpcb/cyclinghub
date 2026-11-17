import base64
import html as _html
import io
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from core.auth import gate
from core.vekta import VektaClient
from core.tp import session_client
from core.riders import parse_riders_csv, cached_csv, store_csv
from core.fitexport import build_workout_fit

st.set_page_config(page_title="Stem Values — WT Performance Hub", page_icon="🎯", layout="wide")
gate()

client = VektaClient()
athletes = client.get_athletes()
if client.last_error:
    st.warning(f"Vekta : {client.last_error} — données de démonstration affichées.")
elif client.notice:
    st.info(client.notice)

st.title("🎯 Stem Values — constructeur de séances")
st.caption("Construis une séance une fois : chaque bloc a sa zone, ses répétitions, sa note "
           "(R45', PP3, follow…) et sa cible — % CP, watts précis, % FCmax ou bpm. "
           "Les watts s'adaptent à chaque coureur. Impression à la carte + fichier compteur .fit.")

# ── Coureurs (source) ─────────────────────────────────────────
src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks", "Fichier CSV"],
                       key="sv_src")
riders = []
if src == "TrainingPeaks":
    tp = session_client()
    if not tp.configured:
        st.sidebar.warning("TrainingPeaks : ajoute TP_USERNAME et TP_PASSWORD dans les "
                           "Secrets Streamlit.")
    else:
        tp_riders = tp.get_athletes()
        if not tp_riders:
            st.sidebar.warning(f"TrainingPeaks : {tp.last_error}")
        else:
            for a in tp_riders:
                cp = tp.get_cp_estimate(a["id"]).get("cp")
                if not cp:
                    continue
                riders.append({"name": a.get("name", a["id"]),
                               "team": a.get("team", a.get("role", "Team")),
                               "hrmax": a.get("hrmax", 180),
                               "weight": a.get("weight", 70), "cp": cp})
elif src == "Vekta (API)":
    for a in athletes:
        pp = client.get_power_profile(a["id"]) or {}
        riders.append({"name": a.get("name", a["id"]),
                       "team": a.get("team", a.get("role", "Team")),
                       "hrmax": a.get("hrmax", 180),
                       "weight": a.get("weight", 70), **pp})
else:
    up = st.sidebar.file_uploader("database_CP.csv", type=["csv"], key="sv_csv")
    if up:
        riders, note = parse_riders_csv(up.getvalue())
        if note and not note.startswith("("):
            st.sidebar.error(note)
        elif riders:
            store_csv(up.name, riders)
            st.sidebar.success(f"{len(riders)} coureur(s) chargé(s) depuis le CSV{note}")
    elif cached_csv():
        cache = cached_csv()
        riders = cache["riders"]
        st.sidebar.info(f"CSV conservé : **{cache['name']}** ({len(riders)} coureur(s)).")

if "sv_manual" not in st.session_state:
    st.session_state["sv_manual"] = []
with st.sidebar.expander("➕ Ajouter des coureurs à la main"):
    with st.form("sv_add_rider", clear_on_submit=True):
        m_name = st.text_input("Nom")
        m_cp = st.number_input("CP (W)", 100, 700, 380)
        mc1, mc2 = st.columns(2)
        m_weight = mc1.number_input("Poids (kg)", 40.0, 110.0, 70.0, 0.5)
        m_hrmax = mc2.number_input("FCmax (bpm)", 150, 220, 185)
        if st.form_submit_button("Ajouter à la liste") and m_name.strip():
            st.session_state["sv_manual"].append({
                "name": m_name.strip(), "team": "Manuel", "cp": int(m_cp),
                "weight": float(m_weight), "hrmax": int(m_hrmax)})
    if st.session_state["sv_manual"]:
        st.write("**Ajoutés à la main :** " + ", ".join(
            r["name"] for r in st.session_state["sv_manual"]))
        if st.button("Vider la liste manuelle", key="sv_manual_clear"):
            st.session_state["sv_manual"] = []
            st.rerun()

riders = riders + [dict(r) for r in st.session_state["sv_manual"]]
if not riders:
    st.info("Choisis une source de coureurs dans la barre de gauche.")
    st.stop()

seen = {}
for r in riders:
    n = str(r.get("name", "Coureur"))
    if n in seen:
        seen[n] += 1
        r["name"] = f"{n} ({seen[n]})"
    else:
        seen[n] = 1
names = [r["name"] for r in riders]
rider_by_name = {r["name"]: r for r in riders}

# ── En-tête stage / camp ──────────────────────────────────────
h1, h2, h3, h4 = st.columns(4)
camp = h1.text_input("Stage / camp (nom)", key="sv_camp")
day = h2.text_input("Jour (ex. J1, J2…)", key="sv_day")
sdate = h3.date_input("Date", datetime.today(), key="sv_date")
logo_up = h4.file_uploader("Logo (impression)", type=["png", "jpg", "jpeg"],
                           key="sv_img_logo")

# ── Zones, cibles, modèles ────────────────────────────────────
ZONE_META = {"Z1": ("Récup", "#94a3b8"), "Z2": ("Endurance", "#16a34a"),
             "Z3": ("Tempo", "#2563eb"), "Z4": ("Seuil", "#d97706"),
             "Z5": ("VO2max", "#dc2626"), "Sprint": ("Sprint", "#ff5722")}
ZONES = list(ZONE_META.keys())
CIBLES = ["% CP", "W", "% FCmax", "bpm", "—"]

ZONE_RULES = {
    "Z1": dict(pct=(30, 65), dmax=90, ddef=30, pdef=45),
    "Z2": dict(pct=(55, 80), dmax=330, ddef=45, pdef=65),
    "Z3": dict(pct=(78, 98), dmax=90, ddef=20, pdef=88),
    "Z4": dict(pct=(94, 115), dmax=40, ddef=20, pdef=100),
    "Z5": dict(pct=(115, 155), dmax=8, ddef=4, pdef=130),
    "Sprint": dict(pct=(160, 300), dmax=1.5, ddef=0.3, pdef=250),
}

TEMPLATES = {
    "Test de terrain 3 × 5 min (max)": [
        ("Échauffement progressif", 1, 20, "Z2", "% CP", 60, ""),
        ("Activation", 1, 4, "Z3", "% CP", 88, ""),
        ("Test 1 — MAX", 1, 5, "Z5", "% CP", 130, "max"),
        ("Récup", 1, 8, "Z1", "% CP", 45, ""),
        ("Test 2 — MAX", 1, 5, "Z5", "% CP", 130, "max"),
        ("Récup", 1, 8, "Z1", "% CP", 45, ""),
        ("Test 3 — MAX", 1, 5, "Z5", "% CP", 130, "max"),
        ("Retour au calme", 1, 15, "Z1", "% CP", 45, "")],
    "Seuil 2 × 20 min": [
        ("Échauffement", 1, 20, "Z2", "% CP", 65, ""),
        ("Activation", 1, 5, "Z3", "% CP", 88, ""),
        ("Seuil", 2, 20, "Z4", "% CP", 100, ""),
        ("Retour au calme", 1, 15, "Z1", "% CP", 45, "")],
    "Tempo 4 × 10 min": [
        ("Échauffement", 1, 20, "Z2", "% CP", 65, ""),
        ("Tempo", 4, 10, "Z3", "% CP", 88, ""),
        ("Récup entre", 3, 5, "Z1", "% CP", 50, ""),
        ("Retour au calme", 1, 15, "Z1", "% CP", 45, "")],
    "Climb + 1' repetitions": [
        ("Climb", 3, 15, "Z3", "bpm", 172, "R45'"),
        ("1' Repetitions", 5, 1, "Z5", "W", 530, "PP3"),
        ("Retour au calme", 1, 15, "Z1", "% CP", 45, "")],
    "Piste / crit : laps & sprints": [
        ("Laps follow", 4, 3, "Z2", "—", 0, "follow"),
        ("Laps in-and-out", 8, 2, "Z3", "—", 0, "in and out"),
        ("Lead-outs", 4, 0.3, "Sprint", "% CP", 250, "lead-out / sprint"),
        ("TT laps", 2, 4, "Z4", "% CP", 100, "TT laps"),
        ("Retour au calme", 1, 15, "Z1", "% CP", 45, "")],
    "Sprints 8 × 10 s": [
        ("Échauffement", 1, 25, "Z2", "% CP", 65, ""),
        ("Activation", 1, 5, "Z3", "% CP", 88, ""),
        ("Sprints", 8, 0.2, "Sprint", "% CP", 250, "assis, vent arrière"),
        ("Récup entre", 7, 2.5, "Z1", "% CP", 45, ""),
        ("Retour au calme", 1, 15, "Z1", "% CP", 45, "")],
    "Récup active 45 min": [
        ("Endurance souple", 1, 45, "Z1", "% CP", 45, "")],
}

COLS = ["bloc", "reps", "min", "zone", "cible", "val", "note"]
DEFAULT_STEPS = pd.DataFrame([list(t) for t in TEMPLATES["Test de terrain 3 × 5 min (max)"]],
                             columns=COLS)
if "sv_steps" not in st.session_state:
    st.session_state["sv_steps"] = DEFAULT_STEPS.copy()
if "sv_prev_zones" not in st.session_state:
    st.session_state["sv_prev_zones"] = DEFAULT_STEPS["zone"].tolist()

tc1, tc2 = st.columns([2, 3])
tpl = tc1.selectbox("Modèle de départ", ["(garder ma séance)"] + list(TEMPLATES.keys()),
                    key="sv_tpl")
if tc2.button("📥 Charger ce modèle dans l'éditeur", key="sv_tpl_load"):
    if tpl != "(garder ma séance)":
        st.session_state["sv_steps"] = pd.DataFrame([list(t) for t in TEMPLATES[tpl]],
                                                    columns=COLS)
        st.session_state["sv_prev_zones"] = st.session_state["sv_steps"]["zone"].tolist()
        st.rerun()

st.subheader("✏️ La séance — un bloc par ligne")
st.caption("Cible : **% CP** = % de la CP du coureur · **W** = watts précis (même valeur pour "
           "tous) · **% FCmax** ou **bpm** = fréquence cardiaque · **—** = libre. "
           "Note = R45', PP3, follow, in and out… Si tu changes la zone, la durée et le "
           "% CP sont corrigés automatiquement s'ils sont incohérents (sinon rien ne bouge).")
try:
    edited = st.data_editor(
        st.session_state["sv_steps"],
        column_config={
            "bloc": st.column_config.TextColumn("Bloc", required=True),
            "reps": st.column_config.NumberColumn("Réps", min_value=1, max_value=30, step=1),
            "min": st.column_config.NumberColumn("Durée/rép (min)", min_value=0.1,
                                                 max_value=480.0, step=0.5),
            "zone": st.column_config.SelectboxColumn("Zone", options=ZONES, required=True),
            "cible": st.column_config.SelectboxColumn("Cible", options=CIBLES,
                                                      required=True),
            "val": st.column_config.NumberColumn("Valeur cible", min_value=0,
                                                 max_value=2000, step=1),
            "note": st.column_config.TextColumn("Note"),
        },
        num_rows="dynamic", use_container_width=True, key="sv_editor")
except Exception:
    st.session_state["sv_steps"] = DEFAULT_STEPS.copy()
    st.session_state["sv_prev_zones"] = DEFAULT_STEPS["zone"].tolist()
    st.warning("Éditeur réinitialisé (état incompatible détecté).")
    st.rerun()

# ── Garde-fous intelligents (zone changée → durée / % CP cohérents) ──
fixes = []
ed = edited.copy()
prev = st.session_state.get("sv_prev_zones") or []
for i in ed.index:
    z = ed.at[i, "zone"]
    if z not in ZONE_RULES:
        continue
    rule = ZONE_RULES[z]
    changed = not (i < len(prev) and prev[i] == z)
    if pd.isna(ed.at[i, "reps"]):
        ed.at[i, "reps"] = 1
    if pd.isna(ed.at[i, "min"]) or (changed and float(ed.at[i, "min"]) > rule["dmax"]):
        old = ed.at[i, "min"]
        ed.at[i, "min"] = rule["ddef"]
        if changed and not pd.isna(old):
            fixes.append(f"« {ed.at[i, 'bloc']} » : {old:g} min est trop long en {z} → "
                         f"{rule['ddef']:g} min")
    if ed.at[i, "cible"] == "% CP":
        v = ed.at[i, "val"]
        lo, hi = rule["pct"]
        if pd.isna(v) or (changed and not (lo <= float(v) <= hi)):
            ed.at[i, "val"] = rule["pdef"]
            if changed and not pd.isna(v):
                fixes.append(f"« {ed.at[i, 'bloc']} » : {v:g} % CP est hors {z} → "
                             f"{rule['pdef']} % CP")
st.session_state["sv_prev_zones"] = ed["zone"].tolist()
st.session_state["sv_steps"] = ed
if fixes:
    st.warning("Corrigé automatiquement (change la valeur si tu veux autre chose) : "
               + " · ".join(fixes))

steps = ed.dropna(subset=["bloc", "zone", "min"]).copy()
steps = steps[steps["min"] > 0]
if steps.empty:
    st.warning("La séance est vide — ajoute des blocs ou charge un modèle.")
    st.stop()
steps["reps"] = steps["reps"].fillna(1)
total_min = float((steps["reps"] * steps["min"]).sum())
st.caption(f"Séance : {len(steps)} blocs · {total_min:.0f} min au total.")


def bloc_label(s):
    reps, mn = float(s["reps"]), float(s["min"])
    if reps > 1:
        return f"{reps:g} × {mn:g}'"
    return f"{mn:g}'"


# ── Coureurs sélectionnés ─────────────────────────────────────
if st.session_state.get("sv_selected"):
    kept = [n for n in st.session_state["sv_selected"] if n in names]
    if kept:
        st.session_state["sv_selected"] = kept
    else:
        del st.session_state["sv_selected"]
selected = st.multiselect(f"Coureurs de la séance ({len(names)} disponibles)", names,
                          default=st.session_state.get("sv_selected", names[:3]),
                          max_selections=30, key="sv_selected")
if not selected:
    st.info("Sélectionne au moins un coureur.")
    st.stop()


def cp_of(name):
    r = rider_by_name[name]
    ov = st.session_state.get(f"sv_cp_{name}")
    try:
        return float(ov) if ov else float(r.get("cp") or 0)
    except (TypeError, ValueError):
        return float(r.get("cp") or 0)


def hrmax_of(name):
    try:
        return float(rider_by_name[name].get("hrmax") or 180)
    except (TypeError, ValueError):
        return 180.0


def watts_of(name, pct):
    cp = cp_of(name)
    adj = float(st.session_state.get(f"sv_adj_{name}", 0) or 0)
    return round(pct / 100.0 * cp * (1 + adj / 100.0))


def target_of(name, s):
    c, v = s["cible"], s["val"]
    if c == "% CP":
        return f"{watts_of(name, float(v))} W"
    if c == "W":
        return f"{float(v):g} W"
    if c == "% FCmax":
        return f"{round(float(v) * hrmax_of(name) / 100)} bpm"
    if c == "bpm":
        return f"{float(v):g} bpm"
    return "libre"


st.subheader("🧍 Adaptation par coureur")
which = st.selectbox("Coureur à régler", selected, key="sv_which")
r = rider_by_name[which]
a1, a2, a3 = st.columns(3)
a1.number_input(f"CP de {which} (W)", 100, 700, int(cp_of(which)), key=f"sv_cp_{which}")
a2.slider("Ajustement global (%)", -20, 20, int(st.session_state.get(f"sv_adj_{which}", 0)),
          key=f"sv_adj_{which}")
a3.metric("Durée totale", f"{total_min:.0f} min")
rows = []
for _, s in steps.iterrows():
    rows.append({"Bloc": s["bloc"], "Zone": s["zone"], "Séance": bloc_label(s),
                 "Note": s["note"] if pd.notna(s["note"]) else "",
                 "Cible": target_of(which, s)})
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ── Fichiers compteurs (.fit) ─────────────────────────────────
st.subheader("📥 Fichiers compteurs (.fit)")


def fit_bytes(name):
    wname = f"{(camp or 'Seance')} {day}".strip()[:14] or "Seance"
    fit_steps = []
    for _, s in steps.iterrows():
        if s["cible"] == "W":
            w = int(float(s["val"]))
        elif s["cible"] == "% CP":
            w = watts_of(name, float(s["val"]))
        else:
            w = 0
        lbl = f"{s['bloc'][:10]} {bloc_label(s)}"
        if w:
            lbl += f" @{w}W"
        fit_steps.append({"name": lbl[:15], "seconds": int(round(float(s["reps"]) *
                                                                 float(s["min"]) * 60)),
                          "watts": w})
    return build_workout_fit(wname, fit_steps)


dl1, dl2 = st.columns(2)
with dl1:
    st.download_button(f"⬇️ Compteur — {which}", fit_bytes(which),
                       file_name=f"seance_{which.replace(' ', '_')}.fit",
                       mime="application/octet-stream")
with dl2:
    if len(selected) > 1:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for nm in selected:
                zf.writestr(f"seance_{nm.replace(' ', '_')}.fit", fit_bytes(nm))
        st.download_button(f"⬇️ Tous les compteurs (.zip, {len(selected)} coureurs)",
                           buf.getvalue(), file_name="seances_compteurs.zip",
                           mime="application/zip")

# ── Document à imprimer ───────────────────────────────────────
st.divider()
st.subheader("🖨️ Document à imprimer")
st.caption("Coche les coureurs à imprimer aujourd'hui (ex. J1 : 4 coureurs, J2 : 20). "
           "Un cartouche par coureur, avec ses cibles à lui.")
pr_cols = st.columns(min(len(selected), 6))
print_sel = []
for i, nm in enumerate(selected):
    with pr_cols[i % len(pr_cols)]:
        if st.checkbox(nm, value=True, key=f"sv_pr_{nm}"):
            print_sel.append(nm)


def _esc(t):
    return _html.escape(str(t if t and t == t else "—"))


def _img_uri(up):
    if up is None:
        return ""
    return "data:" + (up.type or "image/png") + ";base64," + \
        base64.b64encode(up.getvalue()).decode()


PRINT_CSS = """
*{box-sizing:border-box;-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}
body{font-family:Arial,Helvetica,sans-serif;color:#111318;margin:0;padding:18px;font-size:14px}
.printbtn{position:fixed;top:8px;right:8px;background:#111318;color:#fff;border:none;border-radius:6px;padding:10px 18px;font-weight:bold;font-size:14px;cursor:pointer;z-index:99}
@media print{.printbtn{display:none!important}}
.hero{display:flex;align-items:center;gap:16px;background:#111318;color:#fff;border-radius:10px;padding:14px 20px;margin-bottom:14px}
.hero img{max-height:52px;max-width:140px;border-radius:4px;background:#fff;padding:3px}
.kicker{font-size:10px;letter-spacing:2px;text-transform:uppercase;opacity:.6}
.title{font-size:22px;font-weight:900;letter-spacing:1px}
.sub{font-size:12px;opacity:.8}
.tot{margin-left:auto;text-align:center;background:rgba(255,255,255,.12);border-radius:8px;padding:6px 16px}
.tot b{font-size:20px;display:block}
.card{border:1px solid #dde0e8;border-radius:8px;padding:12px 14px;margin-bottom:14px;page-break-inside:avoid}
.card h3{margin:0 0 2px;font-size:16px}
.meta{font-size:11px;color:#7a8194;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#f4f5f7;text-align:left;padding:4px 7px;border-bottom:1px solid #dde0e8;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#7a8194}
td{padding:4px 7px;border-bottom:1px solid #eef0f4;color:#111318}
.zc{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}
.w{font-weight:700;white-space:nowrap}
.grid{display:grid;grid-template-columns:1fr;gap:12px}
"""


def rider_card(nm):
    cp = cp_of(nm)
    r0 = rider_by_name[nm]
    trs = []
    for _, s in steps.iterrows():
        z, col = ZONE_META.get(s["zone"], ("?", "#94a3b8"))
        note = str(s["note"]) if pd.notna(s["note"]) and str(s["note"]).strip() else ""
        trs.append("<tr>"
                   + f'<td><span class="zc" style="background:{col}"></span>'
                     + _esc(s["bloc"]) + "</td>"
                   + f"<td>{z}</td>"
                   + f"<td>{bloc_label(s)}</td>"
                   + f"<td>{_esc(note)}</td>"
                   + f'<td class="w">{target_of(nm, s)}</td>'
                   + "</tr>")
    return (f'<div class="card"><h3>{_esc(nm)}</h3>'
            f'<div class="meta">CP {cp:.0f} W · {float(r0.get("weight") or 0):.0f} kg · '
            f'FCmax {r0.get("hrmax", "—")} · total {total_min:.0f} min</div>'
            f'<table><tr><th>Bloc</th><th>Zone</th><th>Séance</th><th>Note</th>'
            f'<th>Cible</th></tr>{"".join(trs)}</table></div>')


if st.button("👁️ Préparer le document à imprimer", key="sv_print_prep"):
    st.session_state["sv_print_open"] = True
if st.session_state.get("sv_print_open"):
    if st.button("❌ Masquer le document", key="sv_print_hide"):
        st.session_state["sv_print_open"] = False
        st.rerun()
    if not print_sel:
        st.warning("Coche au moins un coureur à imprimer.")
    else:
        logo = _img_uri(logo_up)
        logo_html = f'<img src="{logo}">' if logo else ""
        cards = "".join(rider_card(nm) for nm in print_sel)
        doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{PRINT_CSS}</style></head><body>
<button class="printbtn" onclick="window.print()">🖨️ Imprimer</button>
<div class="hero">{logo_html}<div><div class="kicker">Séance du jour · Stem Values</div>
<div class="title">{_esc(camp or "Séance")}</div>
<div class="sub">{_esc(day)} · {_esc(sdate.strftime("%d/%m/%Y"))} · {len(print_sel)} coureur(s)</div></div>
<div class="tot"><span>Total</span><b>{total_min:.0f}'</b></div></div>
<div class="grid">{cards}</div></body></html>"""
        components.html(doc, height=1000, scrolling=True)

st.caption("Zones ancrées sur la CP de chaque coureur. Aucune donnée ne quitte l'application.")
