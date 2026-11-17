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
st.caption("Construis une séance bloc par bloc : répétitions, durée, zone, cible en plage "
           "(de–à) en % CP, watts, % FCmax ou bpm, et une note (R45', PP3, follow…). "
           "Les watts s'adaptent à chaque coureur. Impression à la carte + fichier .fit.")

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


def _t(bloc, reps, min, zone, cible, vmin, vmax=None, note=""):
    return {"bloc": bloc, "reps": float(reps), "min": float(min), "zone": zone,
            "cible": cible, "vmin": float(vmin),
            "vmax": float(vmin if vmax is None else vmax), "note": note}


TEMPLATES = {
    "Test de terrain 3 × 5 min (max)": [
        _t("Échauffement progressif", 1, 20, "Z2", "% CP", 60),
        _t("Activation", 1, 4, "Z3", "% CP", 88),
        _t("Test 1 — MAX", 1, 5, "Z5", "% CP", 125, 135, "max"),
        _t("Récup", 1, 8, "Z1", "% CP", 45),
        _t("Test 2 — MAX", 1, 5, "Z5", "% CP", 125, 135, "max"),
        _t("Récup", 1, 8, "Z1", "% CP", 45),
        _t("Test 3 — MAX", 1, 5, "Z5", "% CP", 125, 135, "max"),
        _t("Retour au calme", 1, 15, "Z1", "% CP", 45)],
    "Seuil 2 × 20 min": [
        _t("Échauffement", 1, 20, "Z2", "% CP", 65),
        _t("Activation", 1, 5, "Z3", "% CP", 88),
        _t("Seuil", 2, 20, "Z4", "% CP", 98, 102),
        _t("Retour au calme", 1, 15, "Z1", "% CP", 45)],
    "Tempo 4 × 10 min": [
        _t("Échauffement", 1, 20, "Z2", "% CP", 65),
        _t("Tempo", 4, 10, "Z3", "% CP", 85, 90),
        _t("Récup entre", 3, 5, "Z1", "% CP", 50),
        _t("Retour au calme", 1, 15, "Z1", "% CP", 45)],
    "Climb + 1' repetitions": [
        _t("Climb", 3, 15, "Z3", "bpm", 168, 176, "R45'"),
        _t("1' Repetitions", 5, 1, "Z5", "W", 525, 540, "PP3"),
        _t("Retour au calme", 1, 15, "Z1", "% CP", 45)],
    "Piste / crit : laps & sprints": [
        _t("Laps follow", 4, 3, "Z2", "—", 0, 0, "follow"),
        _t("Laps in-and-out", 8, 2, "Z3", "—", 0, 0, "in and out"),
        _t("Lead-outs", 4, 0.3, "Sprint", "% CP", 240, 260, "lead-out / sprint"),
        _t("TT laps", 2, 4, "Z4", "% CP", 98, 102, "TT laps"),
        _t("Retour au calme", 1, 15, "Z1", "% CP", 45)],
    "Sprints 8 × 10 s": [
        _t("Échauffement", 1, 25, "Z2", "% CP", 65),
        _t("Activation", 1, 5, "Z3", "% CP", 88),
        _t("Sprints", 8, 0.2, "Sprint", "% CP", 240, 260, "assis, vent arrière"),
        _t("Récup entre", 7, 2.5, "Z1", "% CP", 45),
        _t("Retour au calme", 1, 15, "Z1", "% CP", 45)],
    "Récup active 45 min": [
        _t("Endurance souple", 1, 45, "Z1", "% CP", 45)],
}

COLS = ["bloc", "reps", "min", "zone", "cible", "vmin", "vmax", "note"]


def _new_id():
    st.session_state["sv_next_id"] = st.session_state.get("sv_next_id", 0) + 1
    return st.session_state["sv_next_id"]


def _blank_bloc():
    return _t("Nouveau bloc", 1, 10, "Z2", "% CP", 65)


if "sv_steps" not in st.session_state or not isinstance(st.session_state["sv_steps"], list):
    st.session_state["sv_steps"] = [_t(**{k: x[k] for k in COLS})
                                    for x in TEMPLATES["Test de terrain 3 × 5 min (max)"]]
    for b in st.session_state["sv_steps"]:
        b["id"] = _new_id()
if "sv_prev_zones" not in st.session_state:
    st.session_state["sv_prev_zones"] = {b["id"]: b["zone"] for b in st.session_state["sv_steps"]}

tc1, tc2 = st.columns([2, 3])
tpl = tc1.selectbox("Modèle de départ", ["(garder ma séance)"] + list(TEMPLATES.keys()),
                    key="sv_tpl")
if tc2.button("📥 Charger ce modèle dans l'éditeur", key="sv_tpl_load"):
    if tpl != "(garder ma séance)":
        for k in list(st.session_state.keys()):
            if k.startswith("sv_b"):
                del st.session_state[k]
        st.session_state["sv_steps"] = [_t(**{k: x[k] for k in COLS}) for x in TEMPLATES[tpl]]
        for b in st.session_state["sv_steps"]:
            b["id"] = _new_id()
        st.session_state["sv_prev_zones"] = {b["id"]: b["zone"]
                                             for b in st.session_state["sv_steps"]}
        st.rerun()

# ── Garde-fous intelligents (zone changée → durée / cible cohérentes) ──
steps_ls = st.session_state["sv_steps"]
fixes = []
for b in steps_ls:
    z = b["zone"]
    rule = ZONE_RULES.get(z)
    if not rule:
        continue
    if st.session_state["sv_prev_zones"].get(b["id"]) != z:
        if b["min"] > rule["dmax"]:
            fixes.append(f"« {b['bloc']} » : {b['min']:g} min est trop long en {z} → "
                         f"{rule['ddef']:g} min")
            b["min"] = rule["ddef"]
            st.session_state.pop(f"sv_b{b['id']}_min", None)
        if b["cible"] == "% CP":
            lo, hi = rule["pct"]
            if not (lo <= b["vmin"] <= hi) or not (lo <= b["vmax"] <= hi):
                fixes.append(f"« {b['bloc']} » : {b['vmin']:g}–{b['vmax']:g} % CP est hors "
                             f"{z} → {rule['pdef']} % CP")
                b["vmin"] = b["vmax"] = rule["pdef"]
                st.session_state.pop(f"sv_b{b['id']}_vmin", None)
                st.session_state.pop(f"sv_b{b['id']}_vmax", None)
        st.session_state["sv_prev_zones"][b["id"]] = z
if fixes:
    st.warning("Corrigé automatiquement (change la valeur si tu veux autre chose) : "
               + " · ".join(fixes))

# ── Éditeur : un bloc par ligne ───────────────────────────────
st.subheader("✏️ La séance — ajoute, enlève, réordonne les blocs librement")
st.caption("Cible **de–à** : % CP (adapté à chaque coureur), W (watts précis, pareil pour "
           "tous), % FCmax ou bpm (cardio), — (libre). Mets la même valeur deux fois pour "
           "une cible unique.")
hd = st.columns([2.2, 0.9, 1.1, 1.1, 1.3, 0.9, 0.9, 1.9, 1.2])
for c, txt in zip(hd, ["Bloc", "Réps", "Durée/rép", "Zone", "Cible", "De", "À", "Note", ""]):
    c.markdown(f'<span style="font-size:11px;font-weight:700;color:#7a8194">'
               f'{txt}</span>', unsafe_allow_html=True)

for idx, b in enumerate(steps_ls):
    uid = b["id"]
    with st.container(border=True):
        c1, c2, c3, c4, c5, c6, c7, c8, c9 = st.columns(
            [2.2, 0.9, 1.1, 1.1, 1.3, 0.9, 0.9, 1.9, 1.2])
        b["bloc"] = c1.text_input("Bloc", b["bloc"], key=f"sv_b{uid}_nm",
                                  label_visibility="collapsed")
        b["reps"] = c2.number_input("Réps", 1, 30, int(b["reps"] or 1), key=f"sv_b{uid}_reps",
                                    label_visibility="collapsed")
        b["min"] = c3.number_input("Durée/rép (min)", 0.1, 480.0, float(b["min"]),
                                   0.1, key=f"sv_b{uid}_min", label_visibility="collapsed")
        b["zone"] = c4.selectbox("Zone", ZONES, index=ZONES.index(b["zone"])
                                 if b["zone"] in ZONES else 1,
                                 key=f"sv_b{uid}_zone", label_visibility="collapsed")
        b["cible"] = c5.selectbox("Cible", CIBLES, index=CIBLES.index(b["cible"])
                                  if b["cible"] in CIBLES else 0,
                                  key=f"sv_b{uid}_cible", label_visibility="collapsed")
        b["vmin"] = c6.number_input("De", 0, 2000, int(b["vmin"] or 0),
                                    key=f"sv_b{uid}_vmin", label_visibility="collapsed")
        b["vmax"] = c7.number_input("À", 0, 2000, int(b["vmax"] or 0),
                                    key=f"sv_b{uid}_vmax", label_visibility="collapsed")
        b["note"] = c8.text_input("Note", b.get("note", ""), key=f"sv_b{uid}_note",
                                  label_visibility="collapsed")
        if c9.button("🗑️", key=f"sv_b{uid}_del", help="Supprimer ce bloc"):
            steps_ls.pop(idx)
            st.rerun()
        if c9.button("▲", key=f"sv_b{uid}_up", disabled=idx == 0,
                     help="Monter ce bloc"):
            steps_ls.insert(idx - 1, steps_ls.pop(idx))
            st.rerun()
        if c9.button("▼", key=f"sv_b{uid}_dn", disabled=idx == len(steps_ls) - 1,
                     help="Descendre ce bloc"):
            steps_ls.insert(idx + 1, steps_ls.pop(idx))
            st.rerun()

ab1, ab2 = st.columns(2)
if ab1.button("➕ Ajouter un bloc", key="sv_add_bloc"):
    nb = _blank_bloc()
    nb["id"] = _new_id()
    steps_ls.append(nb)
    st.rerun()
if ab2.button("🪄 Ajouter une récup après le dernier bloc", key="sv_add_rec"):
    nb = _t("Récup", 1, 5, "Z1", "% CP", 45)
    nb["id"] = _new_id()
    steps_ls.append(nb)
    st.rerun()

steps = [b for b in steps_ls
         if str(b.get("bloc", "")).strip() and float(b.get("min") or 0) > 0]
if not steps:
    st.warning("La séance est vide — ajoute des blocs ou charge un modèle.")
    st.stop()
total_min = sum(float(b["reps"]) * float(b["min"]) for b in steps)
st.caption(f"Séance : {len(steps)} blocs · {total_min:.0f} min au total.")


def bloc_label(b):
    reps, mn = float(b["reps"]), float(b["min"])
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


def _rng(a, b, unit):
    return f"{a} {unit}" if a == b else f"{a}–{b} {unit}"


def target_of(name, b):
    c = b["cible"]
    lo, hi = float(b["vmin"] or 0), float(b["vmax"] or 0)
    if c == "% CP":
        w1, w2 = watts_of(name, lo), watts_of(name, max(lo, hi))
        return _rng(w1, w2, "W")
    if c == "W":
        return _rng(f"{lo:g}", f"{max(lo, hi):g}", "W")
    if c == "% FCmax":
        b1 = round(lo * hrmax_of(name) / 100)
        b2 = round(max(lo, hi) * hrmax_of(name) / 100)
        return _rng(b1, b2, "bpm")
    if c == "bpm":
        return _rng(f"{lo:g}", f"{max(lo, hi):g}", "bpm")
    return "libre"


def _esc(t):
    return _html.escape(str(t if t and str(t).strip() else "—"))


PRINT_CSS = """
*{box-sizing:border-box;-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}
body{font-family:Arial,Helvetica,sans-serif;color:#111318;margin:0;padding:18px;font-size:14px}
.printbtn{position:fixed;top:8px;right:8px;background:#111318;color:#fff;border:none;border-radius:6px;padding:10px 18px;font-weight:bold;font-size:14px;cursor:pointer;z-index:99}
@media print{.printbtn{display:none!important}body{padding:6mm}}
"""

_TD = "padding:4px 7px;border-bottom:1px solid #eef0f4;color:#111318"
_TH = ("background:#f4f5f7;text-align:left;padding:4px 7px;"
       "border-bottom:1px solid #dde0e8;font-size:10px;text-transform:uppercase;"
       "letter-spacing:.5px;color:#7a8194")


def rider_card(nm):
    cp = cp_of(nm)
    r0 = rider_by_name[nm]
    trs = []
    for b in steps:
        z, col = ZONE_META.get(b["zone"], ("?", "#94a3b8"))
        trs.append("<tr>"
                   + f'<td style="{_TD}">'
                     + f'<span style="display:inline-block;width:10px;height:10px;'
                       f'border-radius:2px;background:{col};margin-right:6px;'
                       f'vertical-align:middle"></span>'
                     + _esc(b["bloc"]) + "</td>"
                   + f'<td style="{_TD}">{z}</td>'
                   + f'<td style="{_TD}">{bloc_label(b)}</td>'
                   + f'<td style="{_TD}">{_esc(b.get("note", ""))}</td>'
                   + f'<td style="{_TD};font-weight:700;white-space:nowrap">'
                     f'{target_of(nm, b)}</td>'
                   + "</tr>")
    return ('<div style="border:1px solid #dde0e8;border-radius:8px;padding:12px 14px;'
            'max-width:760px;font-family:Arial,sans-serif;font-size:13px;'
            'margin-bottom:14px">'
            f'<h3 style="margin:0 0 2px;font-size:16px;color:#111318">{_esc(nm)}</h3>'
            f'<div style="font-size:11px;color:#7a8194;margin-bottom:8px">CP {cp:.0f} W · '
            f'{float(r0.get("weight") or 0):.0f} kg · '
            f'FCmax {r0.get("hrmax", "—")} · total {total_min:.0f} min</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            f'<tr><th style="{_TH}">Bloc</th><th style="{_TH}">Zone</th>'
            f'<th style="{_TH}">Séance</th><th style="{_TH}">Note</th>'
            f'<th style="{_TH}">Cible</th></tr>{"".join(trs)}</table></div>')


def rider_sticker(nm):
    cp = cp_of(nm)
    lines = []
    for b in steps:
        z, col = ZONE_META.get(b["zone"], ("?", "#94a3b8"))
        note = _esc(b.get("note", ""))
        note_html = (f'<span style="color:#7a8194">· {note}</span>'
                     if note and note != "—" else "")
        lines.append(
            '<div style="display:flex;justify-content:space-between;'
            'align-items:baseline;gap:5px;padding:2.5px 0;'
            'border-bottom:1px solid #f1f2f6">'
            '<span style="font-size:9.5px;color:#111318;min-width:0">'
            f'<span style="display:inline-block;width:7px;height:7px;'
            f'border-radius:2px;background:{col};margin-right:4px;'
            f'vertical-align:middle"></span>'
            f'<b>{_esc(b["bloc"])}</b> '
            f'<span style="color:#555">{bloc_label(b)}</span> {note_html}</span>'
            f'<b style="font-size:10px;white-space:nowrap;color:#111318">'
            f'{target_of(nm, b)}</b></div>')
    return ('<div style="width:58mm;height:86mm;border:1px dashed #9aa0ae;'
            'border-radius:6px;padding:4mm;box-sizing:border-box;'
            'display:flex;flex-direction:column;background:#fff;'
            'font-family:Arial,sans-serif;page-break-inside:avoid">'
            '<div style="display:flex;justify-content:space-between;'
            'align-items:baseline;border-bottom:2px solid #111318;'
            'padding-bottom:2mm">'
            f'<b style="font-size:13px;color:#111318">{_esc(nm)}</b>'
            f'<span style="font-size:9px;color:#7a8194">CP {cp:.0f}W · '
            f'{total_min:.0f}\'</span></div>'
            f'<div style="flex:1;overflow:hidden;padding-top:2mm">'
            f'{"".join(lines)}</div>'
            '<div style="font-size:8px;color:#7a8194;padding-top:2mm;'
            'border-top:1px solid #e5e7ee;text-transform:uppercase;'
            'letter-spacing:1px">'
            f'{_esc(camp or "Séance")} · {_esc(day)} · '
            f'{_esc(sdate.strftime("%d/%m/%Y"))}</div></div>')



st.subheader("🧍 Adaptation par coureur")
which = st.selectbox("Coureur à régler", selected, key="sv_which")
r = rider_by_name[which]
a1, a2, a3 = st.columns(3)
a1.number_input(f"CP de {which} (W)", 100, 700, int(cp_of(which)), key=f"sv_cp_{which}")
a2.slider("Ajustement global (%)", -20, 20, int(st.session_state.get(f"sv_adj_{which}", 0)),
          key=f"sv_adj_{which}")
a3.metric("Durée totale", f"{total_min:.0f} min")
st.markdown(rider_card(which), unsafe_allow_html=True)

# ── Fichiers compteurs (.fit) ─────────────────────────────────
st.subheader("📥 Fichiers compteurs (.fit)")


def fit_bytes(name):
    wname = f"{(camp or 'Seance')} {day}".strip()[:14] or "Seance"
    fit_steps = []
    for b in steps:
        if b["cible"] == "W":
            w = int(round((float(b["vmin"]) + max(float(b["vmin"]), float(b["vmax"]))) / 2))
        elif b["cible"] == "% CP":
            w = watts_of(name, (float(b["vmin"]) + max(float(b["vmin"]), float(b["vmax"]))) / 2)
        else:
            w = 0
        lbl = f"{b['bloc'][:10]} {bloc_label(b)}"
        if w:
            lbl += f" @{w}W"
        fit_steps.append({"name": lbl[:15],
                          "seconds": int(round(float(b["reps"]) * float(b["min"]) * 60)),
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
st.subheader("🖨️ Autocollants potence à imprimer")
st.caption("Coche les coureurs à imprimer aujourd'hui (ex. J1 : 4 coureurs, J2 : 20). "
           "Un autocollant par coureur (58 × 86 mm), à découper le long des pointillés "
           "et à coller sur la potence du vélo.")
pr_cols = st.columns(min(len(selected), 6))
print_sel = []
for i, nm in enumerate(selected):
    with pr_cols[i % len(pr_cols)]:
        if st.checkbox(nm, value=True, key=f"sv_pr_{nm}"):
            print_sel.append(nm)



def _img_uri(up):
    if up is None:
        return ""
    return "data:" + (up.type or "image/png") + ";base64," + \
        base64.b64encode(up.getvalue()).decode()


if st.button("👁️ Préparer les autocollants à imprimer", key="sv_print_prep"):
    st.session_state["sv_print_open"] = True
if st.session_state.get("sv_print_open"):
    if st.button("❌ Masquer le document", key="sv_print_hide"):
        st.session_state["sv_print_open"] = False
        st.rerun()
    if not print_sel:
        st.warning("Coche au moins un coureur à imprimer.")
    else:
        logo = _img_uri(logo_up)
        logo_html = (f'<img src="{logo}" style="max-height:52px;max-width:140px;'
                     f'border-radius:4px;background:#fff;padding:3px">' if logo else "")
        grid = ('<div style="display:flex;flex-wrap:wrap;gap:4mm">'
                + "".join(rider_sticker(nm) for nm in print_sel) + "</div>")
        doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{PRINT_CSS}</style></head><body>
<button class="printbtn" onclick="window.print()">🖨️ Imprimer</button>
<div style="display:flex;align-items:center;gap:16px;background:#111318;color:#fff;border-radius:10px;padding:14px 20px;margin-bottom:10px">
{logo_html}<div><div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;opacity:.6">Séance du jour · Stem Values</div>
<div style="font-size:22px;font-weight:900;letter-spacing:1px">{_esc(camp or "Séance")}</div>
<div style="font-size:12px;opacity:.8">{_esc(day)} · {_esc(sdate.strftime("%d/%m/%Y"))} · {len(print_sel)} coureur(s) · découpe le long des pointillés</div></div>
<div style="margin-left:auto;text-align:center;background:rgba(255,255,255,.12);border-radius:8px;padding:6px 16px"><span>Total</span><b style="font-size:20px;display:block">{total_min:.0f}'</b></div></div>
{grid}</body></html>"""
        components.html(doc, height=1000, scrolling=True)

st.caption("Zones ancrées sur la CP de chaque coureur. Aucune donnée ne quitte l'application.")
