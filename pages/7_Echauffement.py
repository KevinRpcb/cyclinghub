import base64
import html as _html
import math
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core.auth import gate
from core.config import wu_protocols
from core.vekta import VektaClient
from core.tp import TPClient, session_client
from core.riders import parse_riders_csv, cached_csv, store_csv

st.set_page_config(page_title="Échauffement WU24 — WT Performance Hub", page_icon="🔥", layout="wide")
gate()

client = VektaClient()
athletes = client.get_athletes()
if client.last_error:
    st.warning(f"Vekta : {client.last_error} — données de démonstration affichées.")
elif client.notice:
    st.info(client.notice)

# ════════════════════════════════════════════════════════════════════════
# PROTOCOLES (portage fidèle de WU24 · CP)
# ════════════════════════════════════════════════════════════════════════
# =============================================================
# PROTOCOLES WU24 — TITRES SEULS dans le code public.
# Les détails chiffrés (blocs, %, récup) vivent dans les Secrets
# Streamlit sous la table [WU_PROTOCOLS.*] — voir secrets.toml.example.
# =============================================================
PROTOCOL_TITLES = {
    "standard": ("Standard", "\U0001F7E2"),
    "prologo": ("Prologue / ITT", "\u26a1"),
    "ttt": ("TTT", "\U0001F465"),
    "hot": ("Hot / Nervous start", "\U0001F321\uFE0F"),
    "quiet": ("Controlled start", "\U0001F3D4\uFE0F"),
    "gt": ("Easy 18'", "\U0001F613"),
}

WU_SECRETS_SKELETON = """# Valeurs PLACEHOLDERS — remplace par tes valeurs réelles (privées).
# Répéter le bloc pour chaque protocole : standard, prologo, ttt, hot, quiet, gt.

[WU_PROTOCOLS.standard]
duration = 0
z3blocks = [0, 0, 0]
z3easy = 0
z3pctHR = [0.0, 0.0, 0.0]
z3capPct = 0.0
z4reps = 0
z4work = 0.0
z4rec = 0.0
z4loPct = 0.0
z4hiPct = 0.0
easyAfterZ4 = 0
z5reps = 0
z5work = 0.0
z5rec = 0.0
z5loPct = 0.0
z5hiPct = 0.0
sprintReps = 0
# Optionnel si sprintReps > 0 : sprintWork, sprintRec, sprintEasyBefore
"""

_raw_protos, PROTO_ERROR = wu_protocols()


def _merge_titles(pid, cfg):
    name, emoji = PROTOCOL_TITLES.get(
        pid, (str(cfg.get("name", pid)), str(cfg.get("emoji", "\U0001F4CB"))))
    merged = {"name": name, "emoji": emoji}
    merged.update({k: v for k, v in cfg.items() if k not in ("name", "emoji")})
    return merged


PROTOCOLS = {pid: _merge_titles(pid, cfg) for pid, cfg in _raw_protos.items()}
PROTO_ORDER = ([pid for pid in PROTOCOL_TITLES if pid in PROTOCOLS]
               + [pid for pid in PROTOCOLS if pid not in PROTOCOL_TITLES])
PROTO_ORDER.append("custom")
REF_NAME = {"cp": "CP", "p10s": "P10''", "p20s": "P20''", "p30s": "P30''", "p1m": "P1'",
            "p3m": "P3'", "p5m": "P5'", "p10m": "P10'", "p20m": "P20'"}
REFS = list(REF_NAME.keys())
ZONE_META = {
    "easy": dict(label="Easy", emoji="⬜", color="#94a3b8"),
    "z3": dict(label="Z3", emoji="🟢", color="#16a34a"),
    "z4": dict(label="Z4", emoji="🟡", color="#d97706"),
    "z5": dict(label="Z5", emoji="🔴", color="#dc2626"),
    "sprint": dict(label="Sprint", emoji="🟠", color="#ff5722"),
}


def min_to_str(m):
    tot = round(m * 60)
    mins, secs = divmod(tot, 60)
    if mins > 0 and secs > 0:
        return f"{mins}'{secs}''"
    if mins > 0:
        return f"{mins}'"
    return f"{secs}''"


def sprint_work(p):
    return p.get("sprintWork", 10/60)


def sprint_rec(p):
    return p.get("sprintRec", 50/60)


def easy_start(p):
    return p.get("easyStart", 0)


def calc_all(proto, r, overrides):
    ov = overrides or {}

    def g(field, default):
        return ov.get(field, default)

    return {
        "z3hr": [g(f"z3_hr{i+1}", round(r["hrmax"] * p)) for i, p in enumerate(proto["z3pctHR"])],
        "z3cap": g("z3_cap", round(r["cp"] * proto["z3capPct"])),
        "z4lo": g("z4_lo", round(r["cp"] * proto["z4loPct"])),
        "z4hi": g("z4_hi", round(r["cp"] * proto["z4hiPct"])),
        "z5lo": g("z5_lo", round(r["cp"] * proto["z5loPct"])),
        "z5hi": g("z5_hi", round(r["cp"] * proto["z5hiPct"])),
        "sprintW": g("sprint_ref", r.get("p10s", 0)),
    }


def build_segments(proto, c):
    segs, t = [], 0.0
    if easy_start(proto) > 0:
        segs.append((t, t + easy_start(proto), 80, "easy"))
        t += easy_start(proto)
    fracs = [0.78, 0.88, 1.0] if len(proto["z3blocks"]) == 3 else [0.78, 1.0]
    for dur, f in zip(proto["z3blocks"], fracs):
        segs.append((t, t + dur, round(c["z3cap"] * f), "z3"))
        t += dur
    segs.append((t, t + proto["z3easy"], 80, "easy"))
    t += proto["z3easy"]
    for _ in range(proto["z4reps"]):
        segs.append((t, t + proto["z4work"], c["z4hi"], "z4"))
        t += proto["z4work"]
        segs.append((t, t + proto["z4rec"], 80, "easy"))
        t += proto["z4rec"]
    if proto["easyAfterZ4"] > 0:
        segs.append((t, t + proto["easyAfterZ4"], 80, "easy"))
        t += proto["easyAfterZ4"]
    for _ in range(proto["z5reps"]):
        segs.append((t, t + proto["z5work"], c["z5hi"], "z5"))
        t += proto["z5work"]
        segs.append((t, t + proto["z5rec"], 80, "easy"))
        t += proto["z5rec"]
    if proto["sprintReps"] > 0:
        rem = proto["duration"] - t
        cycle = proto["sprintReps"] * (sprint_work(proto) + sprint_rec(proto))
        easy_bef = proto.get("sprintEasyBefore", max(0.2, (rem - cycle) / 2))
        if easy_bef > 0:
            segs.append((t, t + easy_bef, 80, "easy"))
            t += easy_bef
        for i in range(proto["sprintReps"]):
            segs.append((t, t + sprint_work(proto), c["sprintW"], "sprint"))
            t += sprint_work(proto)
            if i < proto["sprintReps"] - 1:
                segs.append((t, t + sprint_rec(proto), 80, "easy"))
                t += sprint_rec(proto)
        if t < proto["duration"]:
            segs.append((t, proto["duration"], 80, "easy"))
    else:
        segs.append((t, proto["duration"], 80, "easy"))
    return segs


# ── Protocole custom ─────────────────────────────────────────────────────
CUSTOM_DEFAULT = pd.DataFrame([
    {"zone": "z3", "ref": "cp", "pct": 85, "reps": 1, "work_min": 3.0, "rec_min": 0.0, "hr_pct": 0},
    {"zone": "z4", "ref": "cp", "pct": 110, "reps": 3, "work_min": 1.5, "rec_min": 0.5, "hr_pct": 90},
    {"zone": "z5", "ref": "cp", "pct": 124, "reps": 5, "work_min": 0.5, "rec_min": 0.5, "hr_pct": 0},
])


def expand_custom(steps):
    out = []
    for s in steps:
        for i in range(int(s["reps"])):
            out.append((float(s["work_min"]), (s["zone"], s["ref"], float(s["pct"])), s["zone"]))
            if i < int(s["reps"]) - 1 and s["rec_min"] > 0:
                out.append((float(s["rec_min"]), None, "easy"))
    return out


def custom_step_watts(zone, ref, pct, r):
    if zone == "easy":
        return 80
    if zone == "sprint":
        return r.get("p10s", 0)
    return round((r.get(ref) or 0) * pct / 100.0)


def proto_duration_custom(steps):
    return sum(s["reps"] * s["work_min"] + max(0, s["reps"] - 1) * s["rec_min"] for s in steps)


# ── Conditions météo ─────────────────────────────────────────────────────
def assess_conditions(temp_c, rh):
    if temp_c is None or rh is None:
        return None
    e = (rh / 100.0) * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
    at = temp_c + 0.33 * e - 4.0
    if temp_c < 5:
        return dict(level="cold2", label="❄️ Very cold", at=at,
                    actions=["Échauffement +5 min", "Kit thermique complet pendant l'échauffement",
                             "Chauffage passif en zone d'attente", "Manchettes & jambières au départ"])
    if temp_c < 10:
        return dict(level="cold1", label="🧊 Cool", at=at,
                    actions=["Échauffement +2 min", "Rester au chaud pendant l'attente — veste prête"])
    if at < 25:
        return dict(level="ok", label="✅ Comfortable", at=at, actions=[])
    if at < 28:
        return dict(level="warm", label="🌡️ Warm", at=at,
                    actions=["Eau froide & serviettes glacées pendant l'échauffement",
                             "Ombre pendant l'attente", "Hydratation renforcée 30 min avant"])
    if at < 33:
        return dict(level="hot", label="🔥 Hot — Ice vest", at=at,
                    actions=["Gilet glacé pendant l'échauffement", "Serviettes froides nuque/poignets"],
                    note="Envisager le protocole 🌡️ Hot/Nervous")
    return dict(level="extreme", label="🚨 Extreme heat", at=at,
                actions=["Gilet glacé OBLIGATOIRE", "Serviettes froides nuque/poignets",
                         "Passer au protocole 🌡️ Hot/Nervous"],
                note="Passer au protocole 🌡️ Hot/Nervous")


# ════════════════════════════════════════════════════════════════════════
# SOURCES DE DONNÉES : Vekta (API) ou fichier CSV glissé
# ════════════════════════════════════════════════════════════════════════
st.title("🔥 Échauffement WU24 · CP")
st.caption("Portage natif de ton outil : protocoles, éditeur custom, réglage des watts par bloc, "
           "conditions, cartes coureurs et autocollants guidon.")

if PROTO_ERROR or not PROTOCOLS:
    st.error("Protocoles WU24 indisponibles : " + PROTO_ERROR)
    st.markdown("Ajoute la table `[WU_PROTOCOLS]` dans les Secrets "
                "(Streamlit Cloud → App → Settings → Secrets), avec tes valeurs réelles :")
    st.code(WU_SECRETS_SKELETON, language="toml")
    st.stop()

src = st.sidebar.radio("Source des données", ["Vekta (API)", "TrainingPeaks", "Fichier CSV"],
                       key="wu_src")
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
            if not riders:
                st.sidebar.warning("Aucun coureur TP avec un pic puissance 20 ou 60 min "
                                   "trouvé — utilise le CSV ou l'ajout manuel.")
if src == "Vekta (API)":
    for a in athletes:
        pp = client.get_power_profile(a["id"])
        riders.append({"name": a.get("name", a["id"]), "team": a.get("team", a.get("role", "Team")),
                       "hrmax": a.get("hrmax", 180), "weight": a.get("weight", 70), **pp})
elif src == "Fichier CSV":
    up = st.sidebar.file_uploader("database_CP.csv", type=["csv"])
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
        st.sidebar.info(f"CSV conservé de ta dernière visite : **{cache['name']}** "
                        f"({len(riders)} coureur(s)) — glisse un fichier pour le remplacer.")
    else:
        st.info("Glisse ton fichier `database_CP.csv` dans la barre latérale.")

if "wu_manual" not in st.session_state:
    st.session_state["wu_manual"] = []
with st.sidebar.expander("➕ Ajouter des coureurs à la main"):
    st.caption("Utile si Vekta ne liste pas encore tes coureurs ou sans fichier CSV.")
    with st.form("wu_add_rider", clear_on_submit=True):
        m_name = st.text_input("Nom")
        m_cp = st.number_input("CP (W)", 100, 700, 380)
        m_p10s = st.number_input("P10'' (W, sprint)", 300, 2000, int(m_cp * 2.6))
        mc1, mc2 = st.columns(2)
        m_weight = mc1.number_input("Poids (kg)", 40.0, 110.0, 70.0, 0.5)
        m_hrmax = mc2.number_input("FCmax (bpm)", 150, 220, 185)
        if st.form_submit_button("Ajouter à la liste") and m_name.strip():
            st.session_state["wu_manual"].append({
                "name": m_name.strip(), "team": "Manuel", "cp": int(m_cp),
                "p10s": int(m_p10s), "weight": float(m_weight), "hrmax": int(m_hrmax)})
    if st.session_state["wu_manual"]:
        st.write("**Ajoutés à la main :** " + ", ".join(
            r["name"] for r in st.session_state["wu_manual"]))
        if st.button("Vider la liste manuelle"):
            st.session_state["wu_manual"] = []
            st.rerun()

riders = riders + [dict(r) for r in st.session_state["wu_manual"]]

if not riders:
    st.stop()

seen = {}
for r in riders:
    n = str(r.get("name", "Coureur"))
    if n in seen:
        seen[n] += 1
        r["name"] = f"{n} ({seen[n]})"
    else:
        seen[n] = 1

# ════════════════════════════════════════════════════════════════════════
# EN-TÊTE : infos course + conditions
# ════════════════════════════════════════════════════════════════════════
h1, h2, h3, h4 = st.columns(4)
race = h1.text_input("Race", key="wu_race")
stage = h2.text_input("Stage", key="wu_stage")
rdate = h3.date_input("Date", datetime.today(), key="wu_date")
dist = h4.text_input("Distance", key="wu_dist")

t1, t2 = st.columns(2)
temp = t1.number_input("🌡️ Température (°C)", -20.0, 55.0, 20.0, 0.5)
rh = t2.number_input("💧 Humidité (%)", 0, 100, 50)
assessment = assess_conditions(temp, float(rh))
if assessment and assessment["level"] != "ok":
    style = {"cold2": "info", "cold1": "info", "warm": "warning",
             "hot": "error", "extreme": "error"}[assessment["level"]]
    msg = f"**{assessment['label']}** (ressenti {assessment['at']:.0f} °C) — " + \
          " · ".join(assessment["actions"])
    if assessment.get("note"):
        msg += f"\n\n> {assessment['note']}"
    getattr(st, style)(msg)
elif assessment:
    st.success(f"✅ Conditions confortables (ressenti {assessment['at']:.0f} °C)")

with st.expander("🖼️ Logo, profil & carte (pour le document à imprimer)"):
    ic1, ic2, ic3 = st.columns(3)
    ic1.file_uploader("Logo de course", type=["png", "jpg", "jpeg"], key="wu_img_logo")
    ic2.file_uploader("Profil (altimétrie)", type=["png", "jpg", "jpeg"], key="wu_img_profile")
    ic3.file_uploader("Carte du parcours", type=["png", "jpg", "jpeg"], key="wu_img_map")

# ════════════════════════════════════════════════════════════════════════
# PROTOCOLE GLOBAL + ÉDITEUR CUSTOM
# ════════════════════════════════════════════════════════════════════════
pcol, ncol = st.columns([2, 3])
proto_id = pcol.selectbox(
    "Protocole global", PROTO_ORDER,
    format_func=lambda x: (f"✏️ {st.session_state.get('wu_custom_name', 'Custom')}" if x == "custom"
                           else f"{PROTOCOLS[x]['emoji']} {PROTOCOLS[x]['name']} — {PROTOCOLS[x]['duration']}'"))

ncol.text_input("Nom du protocole custom", key="wu_custom_name")
if "wu_custom_steps" not in st.session_state:
    st.session_state["wu_custom_steps"] = CUSTOM_DEFAULT.copy()

with st.expander("✏️ Éditeur de protocole custom", expanded=proto_id == "custom"):
    try:
        edited = st.data_editor(
            st.session_state["wu_custom_steps"],
            column_config={
                "zone": st.column_config.SelectboxColumn("Zone", options=list(ZONE_META.keys()), required=True),
                "ref": st.column_config.SelectboxColumn("Référence", options=REFS, required=True),
                "pct": st.column_config.NumberColumn("% réf.", min_value=30, max_value=200, step=1),
                "reps": st.column_config.NumberColumn("Répétitions", min_value=1, max_value=12, step=1),
                "work_min": st.column_config.NumberColumn("Travail (min)", min_value=0.1, max_value=15.0, step=0.1),
                "rec_min": st.column_config.NumberColumn("Récup (min)", min_value=0.0, max_value=5.0, step=0.1),
                "hr_pct": st.column_config.NumberColumn("% FCmax", min_value=0, max_value=95, step=1),
            },
            num_rows="dynamic", use_container_width=True)
    except Exception:
        st.session_state["wu_custom_steps"] = CUSTOM_DEFAULT.copy()
        st.warning("Éditeur réinitialisé (état incompatible détecté).")
        st.rerun()
    st.session_state["wu_custom_steps"] = edited
    ca, cb = st.columns(2)
    if ca.button("↺ Réinitialiser l'éditeur"):
        st.session_state["wu_custom_steps"] = CUSTOM_DEFAULT.copy()
        st.rerun()
    cb.download_button("⬇️ Exporter en JSON",
                       edited.to_json(orient="records", indent=2),
                       f"{st.session_state.get('wu_custom_name') or 'custom_protocol'}.json",
                       "application/json")

custom_steps = edited.to_dict("records")
if proto_id == "custom" and not custom_steps:
    st.warning("Le protocole custom est vide — ajoute des blocs dans l'éditeur.")
    st.stop()

# ════════════════════════════════════════════════════════════════════════
# COUREURS (slots) + SURCHARGE PAR COUREUR
# ════════════════════════════════════════════════════════════════════════
names = [r["name"] for r in riders]
if st.session_state.get("wu_selected"):
    kept = [n for n in st.session_state["wu_selected"] if n in names]
    if kept:
        st.session_state["wu_selected"] = kept
    else:
        del st.session_state["wu_selected"]
selected = st.multiselect(f"Coureurs sélectionnés ({len(names)} disponibles — max 20)", names,
                          default=st.session_state.get("wu_selected", names[:3]),
                          max_selections=20, key="wu_selected")
if not selected:
    st.info("Sélectionne au moins un coureur.")
    st.stop()


def get_proto_for(rider_name):
    ov_id = st.session_state.get(f"wu_override_{rider_name}", proto_id)
    if ov_id == "custom":
        return "custom", dict(name=st.session_state.get("wu_custom_name") or "Custom", emoji="✏️",
                              duration=round(proto_duration_custom(custom_steps), 1))
    return ov_id, PROTOCOLS[ov_id]


def rider_by_name(name):
    return next(r for r in riders if r["name"] == name)


def rider_plan(name):
    r = rider_by_name(name)
    ov_id, proto = get_proto_for(name)
    ov = st.session_state.setdefault(f"wu_ov_{name}", {})
    if ov_id == "custom":
        segs, t = [], 0.0
        for dur, spec, zone in expand_custom(custom_steps):
            w = 80 if spec is None else custom_step_watts(*spec, r)
            segs.append((t, t + dur, w, "easy" if spec is None else spec[0]))
            t += dur
        cvals = None
        blocks = []
        for s in custom_steps:
            zm = ZONE_META[s["zone"]]
            w = custom_step_watts(s["zone"], s["ref"], s["pct"], r)
            lbl = (f"{int(s['reps'])}×{min_to_str(s['work_min'])}" if s["reps"] > 1
                   else min_to_str(s["work_min"]))
            if s["rec_min"] > 0 and s["reps"] > 1:
                lbl += f" r{min_to_str(s['rec_min'])}"
            blocks.append((zm, lbl, w))
    else:
        cvals = calc_all(proto, r, ov)
        segs = build_segments(proto, cvals)
        z3dur = sum(proto["z3blocks"]) + proto["z3easy"]
        z4str = f"{proto['z4reps']}×{min_to_str(proto['z4work'])} r{min_to_str(proto['z4rec'])}"
        z5str = f"{proto['z5reps']}×{min_to_str(proto['z5work'])}/{min_to_str(proto['z5rec'])}"
        blocks = [
            (ZONE_META["z3"], f"{min_to_str(z3dur)} progression Z3 ({'+'.join(min_to_str(b) for b in proto['z3blocks'])})", cvals["z3cap"]),
            (ZONE_META["z4"], f"{z4str} Z4", cvals["z4hi"]),
        ]
        if proto["z5reps"] > 0:
            blocks.append((ZONE_META["z5"], f"{z5str} Z5", cvals["z5hi"]))
        if proto["sprintReps"] > 0:
            blocks.append((ZONE_META["sprint"],
                           f"{proto['sprintReps']}×{min_to_str(sprint_work(proto))} Sprint assis", None))
    return r, ov_id, proto, cvals, segs, blocks


def _wu_fit_bytes(r, segs):
    from core.fitexport import build_workout_fit
    steps = []
    for t0, t1, w, z in segs:
        sec = int(round((t1 - t0) * 60))
        if sec <= 0:
            continue
        if steps and steps[-1]["watts"] == int(w or 0):
            steps[-1]["seconds"] += sec
            continue
        steps.append({"name": f"{str(z).upper()} {sec // 60}'{sec % 60:02d}",
                      "seconds": sec, "watts": int(w or 0)})
    return build_workout_fit(f"WU24 {r['name']}", steps)


for name in selected:
    r, ov_id, proto, cvals, segs, blocks = rider_plan(name)
    ov = st.session_state.setdefault(f"wu_ov_{name}", {})

    with st.container(border=True):
        head_l, head_r = st.columns([3, 2])
        head_l.markdown(f"### {proto['emoji']} {r['name']} — {proto['duration']}' échauffement")
        head_l.caption(f"{race or '—'} · {stage or '—'} · {rdate.strftime('%d/%m/%Y')} · {dist or '—'} · "
                       f"FCmax {r['hrmax']} · {float(r['weight']):.1f} kg · CP {r['cp']} W")
        override = head_r.selectbox(
            "Protocole du coureur", PROTO_ORDER, index=PROTO_ORDER.index(ov_id),
            format_func=lambda x: (f"✏️ {st.session_state.get('wu_custom_name', 'Custom')}" if x == "custom"
                                   else f"{PROTOCOLS[x]['emoji']} {PROTOCOLS[x]['name']}"),
            key=f"wu_override_{name}",
            help="Surcharge locale : ce protocole ne s'applique qu'à ce coureur.")
        if override != ov_id:
            st.session_state[f"wu_override_{name}"] = override
            st.session_state[f"wu_ov_{name}"] = {}
            st.rerun()

        fig = go.Figure()
        xs, ys = [0.0], [80]
        for t0, t1, w, z in segs:
            xs += [t0, t1]
            ys += [w, w]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="Puissance",
                                 line=dict(color="#475569", width=2)))
        fig.update_layout(height=200, template="plotly_white", margin=dict(l=10, r=10, t=10, b=10),
                          xaxis_title="Minutes", yaxis_title="W")
        st.plotly_chart(fig, use_container_width=True)

        adj_l, adj_r = st.columns([3, 2])
        with adj_l:
            for zm, lbl, w in blocks:
                if w is None:
                    st.markdown(f"{zm['emoji']} **{lbl}** — MAX assis · {r.get('p10s', 0)} W")
                else:
                    st.markdown(f"{zm['emoji']} **{lbl}** — {zm['label']} · **{w} W**")
            if cvals:
                st.caption(f"Z3 : cap ≤{cvals['z3cap']} W · HR {'→'.join(str(v) for v in cvals['z3hr'])} bpm · "
                           f"Z4 : {cvals['z4lo']}–{cvals['z4hi']} W · Z5 : {cvals['z5lo']}–{cvals['z5hi']} W · "
                           f"Sprint ref {cvals['sprintW']} W")
        with adj_r:
            with st.popover(f"🎛️ Régler les watts — {r['name']}"):
                if ov_id == "custom":
                    st.caption("Ajuste les % de référence dans l'éditeur custom ci-dessus.")
                else:
                    for fkey, flbl, default in [("z3_cap", "Cap Z3", cvals["z3cap"]),
                                                ("z4_lo", "Z4 bas", cvals["z4lo"]),
                                                ("z4_hi", "Z4 haut", cvals["z4hi"]),
                                                ("z5_lo", "Z5 bas", cvals["z5lo"]),
                                                ("z5_hi", "Z5 haut", cvals["z5hi"])]:
                        pct_def = round(default / r["cp"] * 100)
                        pct = st.slider(f"{flbl} (% CP {r['cp']} W)",
                                        max(1, pct_def - 20), pct_def + 20, pct_def,
                                        key=f"wu_slider_{name}_{fkey}")
                        val = round(r["cp"] * pct / 100)
                        if val != default:
                            ov[fkey] = val
                        else:
                            ov.pop(fkey, None)
                    if st.button("↺ Réinitialiser", key=f"wu_reset_{name}"):
                        st.session_state[f"wu_ov_{name}"] = {}
                        st.rerun()
            st.download_button("⬇️ Compteur (.fit)", _wu_fit_bytes(r, segs),
                               f"warmup_{r['name'].replace(' ', '_')}.fit",
                               "application/octet-stream", key=f"wu_fit_{name}")

# ─── Document à imprimer (page 1 : course + images + grand graphique ; pages suivantes : autocollants) ───
_FILL = {"z3": "#bbf7d0", "z4": "#fef08a", "z5": "#fecaca", "easy": "#f1f5f9", "sprint": "#ffccbc"}
_STROKE = {"z3": "#16a34a", "z4": "#d97706", "z5": "#dc2626", "easy": "#cbd5e1", "sprint": "#ff5722"}

_PRINT_CSS = """
@page{size:A4 portrait;margin:10mm}
*{box-sizing:border-box;-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}
body{font-family:Arial,Helvetica,sans-serif;color:#111318;margin:0;background:#fff;font-size:12px}
.printbtn{position:fixed;top:8px;right:8px;background:#111318;color:#fff;border:none;border-radius:6px;padding:10px 18px;font-weight:bold;font-size:14px;cursor:pointer;z-index:99}
@media print{.printbtn{display:none!important}}
.hero{background:#111318;color:#fff;border-radius:3mm;padding:4mm 5mm;display:flex;align-items:center;gap:5mm}
.hero img.logo{max-height:15mm;max-width:26mm;object-fit:contain}
.logo-ph{min-width:22mm;height:12mm;border:1px solid rgba(255,255,255,.4);border-radius:2mm;display:flex;align-items:center;justify-content:center;color:rgba(255,255,255,.5);font-size:9px}
.hero-mid{flex:1}
.kicker{font-size:9px;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.6)}
.title{font-size:20px;font-weight:900;letter-spacing:1px}
.sub{font-size:11px;color:rgba(255,255,255,.7)}
.tot{text-align:center;background:rgba(255,255,255,.12);border-radius:2mm;padding:2mm 4mm}
.tot span{display:block;font-size:8px;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.6)}
.tot b{font-size:20px}
.tot i{display:block;font-style:normal;font-size:9px;color:#f7e982}
.racebar{display:flex;border:1px solid #dde0e8;border-top:none;border-radius:0 0 2mm 2mm;margin-bottom:3mm}
.racebar div{flex:1;padding:2mm 3mm;border-right:1px solid #dde0e8}
.racebar div:last-child{border-right:none}
.racebar i{display:block;font-style:normal;font-size:8px;letter-spacing:1px;text-transform:uppercase;color:#7a8194}
.racebar b{font-size:12px}
.media{display:flex;gap:3mm;margin-bottom:3mm}
.panel{flex:1;border:1px solid #dde0e8;border-radius:2mm;padding:2mm}
.panel i{display:block;font-style:normal;font-size:8px;letter-spacing:1px;text-transform:uppercase;color:#7a8194;margin-bottom:1mm}
.panel img{width:100%;height:42mm;object-fit:cover;border-radius:1mm}
.ph{height:42mm;border:1px dashed #c8ccd6;border-radius:1mm;display:flex;align-items:center;justify-content:center;color:#a0a6b5;font-size:10px}
.chartbox{border:1px solid #dde0e8;border-radius:2mm;padding:2mm;margin-bottom:3mm}
.chartbox i{display:block;font-style:normal;font-size:8px;letter-spacing:1px;text-transform:uppercase;color:#7a8194;margin-bottom:1mm}
.pills{display:flex;flex-wrap:wrap;gap:2mm}
.pill{color:#fff;border-radius:2mm;padding:1.5mm 3mm;min-width:30mm}
.pill b{display:block;font-size:11px}
.pill span{font-size:8px;opacity:.9}
.cond{margin-top:3mm;border:1px solid #fecaca;background:#fef2f2;color:#b91c1c;border-radius:2mm;padding:2mm 3mm;font-size:10px;font-weight:bold}
.pagebreak{page-break-after:always}
.stk-title{font-size:14px;letter-spacing:1px;text-transform:uppercase;margin:0 0 1mm}
.stk-sub{font-size:10px;color:#7a8194;margin:0 0 3mm}
.stickers{display:flex;flex-wrap:wrap;gap:4mm}
.sticker{width:58mm;height:86mm;border:1px dashed #94a3b8;border-radius:2mm;overflow:hidden;page-break-inside:avoid;display:flex;flex-direction:column}
.sk-hdr{background:#111318;color:#fff;padding:2mm 3mm;display:flex;justify-content:space-between;align-items:baseline}
.sk-hdr span{font-size:8px;letter-spacing:1px;text-transform:uppercase;color:#f7e982}
.sk-hdr b{font-size:13px;text-transform:uppercase}
.sk-body{padding:1mm 0;flex:1}
.band{display:flex;justify-content:space-between;align-items:center;padding:1.4mm 2.5mm;color:#fff;font-size:9px;font-weight:bold;margin:1mm 1.5mm;border-radius:1.5mm}
.band b{font-size:10px}
.sk-hr{padding:1mm 2.5mm;font-size:8px;color:#475569;text-align:center}
.sk-foot{padding:1.5mm 2.5mm;font-size:8px;color:#7a8194;border-top:1px solid #dde0e8}
"""


def _img_uri(up):
    if up is None:
        return ""
    return "data:" + (up.type or "image/png") + ";base64," + base64.b64encode(up.getvalue()).decode()


def _esc(txt):
    return _html.escape(str(txt if txt else "—"))


def _svg_chart(segs, duration, w=560, h=130, pl=44, pr=16, pt=12, pb=26, fs=10):
    cw, ch = w - pl - pr, h - pt - pb
    maxw = max([s[2] for s in segs] + [1]) * 1.08
    def tx(t):
        return pl + (t / max(float(duration), 0.1)) * cw
    def ty(v):
        return pt + ch - (min(v, maxw) / maxw) * ch
    bars, pts = [], []
    for t0, t1, v, z in segs:
        x1, x2 = tx(t0), tx(t1)
        bars.append(f'<rect x="{x1:.1f}" y="{ty(v):.1f}" width="{x2 - x1:.1f}" '
                    f'height="{ty(0) - ty(v):.1f}" fill="{_FILL[z]}" stroke="{_STROKE[z]}" stroke-width="0.4"/>')
        pts.append(f"{x1:.1f},{max(pt, ty(v)):.1f} {x2:.1f},{max(pt, ty(v)):.1f}")
    ticks = list(range(0, int(duration) + 1, 5))
    if round(duration) not in ticks:
        ticks.append(round(duration))
    tl = "".join(f'<text x="{tx(t):.1f}" y="{h - 4}" text-anchor="middle" fill="#64748b" '
                 f'font-size="{fs}" font-family="Arial">{t}\'</text>' for t in ticks)
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">'
            f'<rect x="{pl}" y="{pt}" width="{cw}" height="{ch}" fill="#f8fafc"/>{"".join(bars)}'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="#64748b" stroke-width="0.8"/>'
            f'<line x1="{pl}" y1="{pt + ch}" x2="{pl + cw}" y2="{pt + ch}" stroke="#cbd5e1" stroke-width="1"/>{tl}</svg>')


def _proto_pills(proto):
    es = easy_start(proto)
    z3d = sum(proto["z3blocks"]) + proto["z3easy"]
    z4d = proto["z4reps"] * (proto["z4work"] + proto["z4rec"])
    e = proto["easyAfterZ4"]
    z5d = proto["z5reps"] * (proto["z5work"] + proto["z5rec"])
    sp = max(0.0, proto["duration"] - es - z3d - z4d - e - z5d)
    pills = []
    if es > 0:
        pills.append(("#64748b", f"{min_to_str(es)} Easy", "départ progressif"))
    pills.append(("#16a34a", f"{min_to_str(z3d)} Z3",
                  f"{'+'.join(min_to_str(b) for b in proto['z3blocks'])} + {min_to_str(proto['z3easy'])} easy · HR "
                  + "→".join(f"{round(p * 100)}%" for p in proto["z3pctHR"])))
    pills.append(("#d97706", f"{min_to_str(z4d)} Z4",
                  f"{proto['z4reps']}×{min_to_str(proto['z4work'])} r{min_to_str(proto['z4rec'])} · "
                  f"{round(proto['z4loPct'] * 100)}–{round(proto['z4hiPct'] * 100)}% CP"))
    if e > 0:
        pills.append(("#64748b", f"{min_to_str(e)} Easy", "récup active"))
    if proto["z5reps"] > 0:
        pills.append(("#dc2626", f"{min_to_str(z5d)} Z5",
                      f"{proto['z5reps']}×{min_to_str(proto['z5work'])}/{min_to_str(proto['z5rec'])} · "
                      f"{round(proto['z5loPct'] * 100)}% CP"))
    pills.append(("#ff5722",
                  f"{proto['sprintReps']}×10'' Sprint" if proto["sprintReps"] > 0 else "Sans sprint",
                  f"{min_to_str(sp)} easy · assis"))
    return pills


def _custom_pills(steps):
    grouped = {}
    for dur, spec, zone in expand_custom(steps):
        grouped[zone] = grouped.get(zone, 0.0) + dur
    return [(ZONE_META[z]["color"], f"{min_to_str(d)} {ZONE_META[z]['label']}", ZONE_META[z]["emoji"])
            for z, d in grouped.items()]


def _sticker_html(r, proto, cvals, segs, blocks):
    chart = _svg_chart(segs, proto["duration"], w=154, h=46, pl=3, pr=3, pt=2, pb=8, fs=5)
    band_html = []
    for zm, lbl, w in blocks:
        wtxt = "MAX" if w is None else f"{w} W"
        band_html.append(f'<div class="band" style="background:{zm["color"]}">'
                         f'<span>{zm["emoji"]} {_esc(lbl)}</span><b>{wtxt}</b></div>')
    hr = ""
    if cvals:
        hr = (f'<div class="sk-hr">HR Z3 {"→".join(str(v) for v in cvals["z3hr"])} bpm · '
              f'Z4 {cvals["z4lo"]}–{cvals["z4hi"]} W · Z5 {cvals["z5lo"]}–{cvals["z5hi"]} W</div>')
    return (f'<div class="sticker"><div class="sk-hdr"><span>{proto["duration"]}\' warm-up</span>'
            f'<b>{_esc(r["name"])}</b></div>{chart}<div class="sk-body">{"".join(band_html)}{hr}</div>'
            f'<div class="sk-foot">CP {r["cp"]} W · {float(r["weight"]):.0f} kg · FCmax {r["hrmax"]}</div></div>')


def build_print_doc(race, stage, date_str, dist, proto_label, gduration, pills, bus_chart,
                    plans, images, assessment):
    logo, profile, carte = images
    logo_html = f'<img class="logo" src="{logo}">' if logo else '<div class="logo-ph">Logo</div>'

    def media(uri, label):
        return f'<img src="{uri}">' if uri else f'<div class="ph">{label}</div>'

    cond = ""
    if assessment and assessment["level"] != "ok":
        cond = (f'<div class="cond">{_esc(assessment["label"])} — '
                f'{_esc(" · ".join(assessment["actions"]))}</div>')
    pills_html = "".join(
        f'<div class="pill" style="background:{color}"><b>{_esc(main)}</b><span>{_esc(sub)}</span></div>'
        for color, main, sub in pills)
    stickers = "".join(_sticker_html(p[0], p[2], p[3], p[4], p[5]) for p in plans)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_PRINT_CSS}</style></head><body>
<button class="printbtn" onclick="window.print()">🖨️ Imprimer</button>
<div class="hero">{logo_html}<div class="hero-mid"><div class="kicker">Protocole du jour</div>
<div class="title">Échauffement WU24</div><div class="sub">{_esc(race)} · {_esc(stage)}</div></div>
<div class="tot"><span>Total</span><b>{gduration}'</b><i>{_esc(proto_label)}</i></div></div>
<div class="racebar">
<div><i>Course</i><b>{_esc(race)}</b></div><div><i>Étape</i><b>{_esc(stage)}</b></div>
<div><i>Date</i><b>{_esc(date_str)}</b></div><div><i>Distance</i><b>{_esc(dist)}</b></div></div>
<div class="media"><div class="panel"><i>Profil</i>{media(profile, "Ajoute le profil dans l'app")}</div>
<div class="panel"><i>Carte</i>{media(carte, "Ajoute la carte dans l'app")}</div></div>
<div class="chartbox"><i>Allure du protocole — {gduration} min</i>{bus_chart}</div>
<div class="pills">{pills_html}</div>{cond}</div>
<div class="pagebreak"></div>
<h2 class="stk-title">{_esc(race)} — {_esc(stage)}</h2>
<p class="stk-sub">{_esc(date_str)} · échauffement {gduration}' · découpe le long des pointillés</p>
<div class="stickers">{stickers}</div></body></html>"""


_pseudo = {"cp": 400, "hrmax": 200, "p10s": 1000, "weight": 70.0}
if proto_id == "custom":
    bus_segs, _bt = [], 0.0
    for dur, spec, zone in expand_custom(custom_steps):
        w = 80 if spec is None else custom_step_watts(*spec, _pseudo)
        bus_segs.append((_bt, _bt + dur, w, "easy" if spec is None else spec[0]))
        _bt += dur
    bus_pills = _custom_pills(custom_steps)
    gduration = round(proto_duration_custom(custom_steps), 1)
    proto_label = f"✏️ {st.session_state.get('wu_custom_name') or 'Custom'}"
else:
    gp = PROTOCOLS[proto_id]
    bus_segs = build_segments(gp, calc_all(gp, _pseudo, {}))
    bus_pills = _proto_pills(gp)
    gduration = gp["duration"]
    proto_label = f"{gp['emoji']} {gp['name']}"
bus_chart = _svg_chart(bus_segs, gduration)

st.divider()
st.subheader("🖨️ Document à imprimer")
st.caption("Page 1 : course, logo, profil, carte + grand graphique. Pages suivantes : un autocollant "
           "par coureur sélectionné (58×86 mm, découpe). Clique sur « 🖨️ Imprimer » dans le document.")
if st.button("👁️ Préparer le document à imprimer", key="wu_print_prep"):
    st.session_state["wu_print_open"] = True
if st.session_state.get("wu_print_open"):
    if st.button("❌ Masquer le document", key="wu_print_hide"):
        st.session_state["wu_print_open"] = False
        st.rerun()
    images = tuple(_img_uri(st.session_state.get(k))
                   for k in ("wu_img_logo", "wu_img_profile", "wu_img_map"))
    plans = [rider_plan(n) for n in selected]
    doc = build_print_doc(race, stage, rdate.strftime("%d/%m/%Y"), dist, proto_label, gduration,
                          bus_pills, bus_chart, plans, images, assessment)
    components.html(doc, height=1100, scrolling=True)

st.divider()
st.caption("Zones ancrées sur la CP du coureur (source : " +
           ("API Vekta" if src == "Vekta (API)" else "database_CP.csv") + "). "
           "Aucune donnée ne quitte l'application.")
