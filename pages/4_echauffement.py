import io
import math
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.auth import gate
from core.config import wu_protocols
from core.vekta import VektaClient

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

src = st.sidebar.radio("Source des données", ["Vekta (API)", "Fichier CSV"], key="wu_src")
riders = []
if src == "Vekta (API)":
    for a in athletes:
        pp = client.get_power_profile(a["id"])
        riders.append({"name": a.get("name", a["id"]), "team": a.get("team", a.get("role", "Team")),
                       "hrmax": a.get("hrmax", 180), "weight": a.get("weight", 70), **pp})
else:
    up = st.sidebar.file_uploader("database_CP.csv", type=["csv"])
    if up:
        try:
            raw = up.getvalue().decode("utf-8-sig")
            sep = ";" if ";" in raw.splitlines()[0] else ","
            df = pd.read_csv(io.StringIO(raw), sep=sep)
            df.columns = [c.strip().lower() for c in df.columns]
            required = ["name", "hrmax", "weight", "cp"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                st.error(f"Colonnes manquantes : {', '.join(missing)}")
            else:
                riders = df.to_dict("records")
        except Exception as e:
            st.error(f"CSV illisible : {e}")
    else:
        st.info("Glisse ton fichier `database_CP.csv` dans la barre latérale.")

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
    edited = st.data_editor(
        st.session_state["wu_custom_steps"],
        columns={
            "zone": st.column_config.SelectboxColumn("Zone", options=list(ZONE_META.keys()), required=True),
            "ref": st.column_config.SelectboxColumn("Référence", options=REFS, required=True),
            "pct": st.column_config.NumberColumn("% réf.", min_value=30, max_value=200, step=1),
            "reps": st.column_config.NumberColumn("Répétitions", min_value=1, max_value=12, step=1),
            "work_min": st.column_config.NumberColumn("Travail (min)", min_value=0.1, max_value=15.0, step=0.1),
            "rec_min": st.column_config.NumberColumn("Récup (min)", min_value=0.0, max_value=5.0, step=0.1),
            "hr_pct": st.column_config.NumberColumn("% FCmax", min_value=0, max_value=95, step=1),
        },
        num_rows="dynamic", use_container_width=True, key="wu_custom_editor")
    st.session_state["wu_custom_steps"] = edited
    ca, cb = st.columns(2)
    if ca.button("↺ Réinitialiser l'éditeur"):
        st.session_state["wu_custom_steps"] = CUSTOM_DEFAULT.copy()
        st.rerun()
    cb.download_button("⬇️ Exporter en JSON",
                       edited.to_json(orient="records", indent=2),
                       f"{st.session_state.get('wu_custom_name') or 'custom_protocol'}.json",
                       "application/json")

custom_steps = []
if proto_id == "custom":
    custom_steps = edited.to_dict("records")
    if not custom_steps:
        st.warning("Le protocole custom est vide — ajoute des blocs dans l'éditeur.")
        st.stop()

# ════════════════════════════════════════════════════════════════════════
# COUREURS (slots) + SURCHARGE PAR COUREUR
# ════════════════════════════════════════════════════════════════════════
names = [r["name"] for r in riders]
selected = st.multiselect("Coureurs sélectionnés (max 9)", names,
                          default=st.session_state.get("wu_selected", names[:3]),
                          max_selections=9, key="wu_selected")
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


for name in selected:
    r = rider_by_name(name)
    ov_id, proto = get_proto_for(name)
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

        with st.expander("✂️ Autocollant guidon (4×7 cm)"):
            cond_strip = ""
            if assessment and assessment["level"] in ("hot", "extreme", "cold2"):
                cond_strip = f"**{assessment['label']}**  \n"
            lines = [f"{cond_strip}**{r['name']}** · {proto['duration']}' warm-up"]
            for zm, lbl, w in blocks:
                watts = "MAX" if w is None else f"{w} W"
                lines.append(f":{zm['color']}[{zm['emoji']} {lbl} — {zm['label']} `{watts}`]")
            if cvals:
                lines.append("HR Z3 : " + " → ".join(str(v) for v in cvals["z3hr"]) + " bpm")
            st.markdown("  \n".join(lines))
            st.caption("Imprime cette section (Ctrl+P) et découpe le long des bordures.")

st.divider()
st.caption("Zones ancrées sur la CP du coureur (source : " +
           ("API Vekta" if src == "Vekta (API)" else "database_CP.csv") + "). "
           "Aucune donnée ne quitte l'application.")
