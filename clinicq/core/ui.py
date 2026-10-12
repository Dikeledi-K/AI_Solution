"""Shared look and feel for every ClinicQ page.

Streamlit's widgets get their base colours from .streamlit/config.toml. This file
adds the rest: a CSS theme, gradient page headers, coloured stat tiles, badges
and the disclaimer banner. Pages build their HTML with these helpers so the
design stays consistent.

Any text that comes from a patient or the AI is passed through html.escape()
before it is put into HTML, so it can never inject markup into the page.
"""

from html import escape

import streamlit as st

from core.database import count_open_alerts
from core.llm_client import get_provider_name

DISCLAIMER = (
    "ClinicQ supports clinic staff and does not diagnose. "
    "In an emergency, go straight to the nurse or call 10177."
)

# Colours for each urgency level: solid colour, soft background, text on solid.
URGENCY_STYLE = {
    "RED": {"bg": "#e53935", "soft": "#fdecea", "fg": "#ffffff", "label": "🔴 RED – Emergency",
            "gradient": "linear-gradient(135deg,#f87171,#dc2626)"},
    "ORANGE": {"bg": "#f57c00", "soft": "#fff3e0", "fg": "#ffffff", "label": "🟠 ORANGE – Very urgent",
               "gradient": "linear-gradient(135deg,#fdba74,#ea580c)"},
    "YELLOW": {"bg": "#f9c80e", "soft": "#fffbea", "fg": "#3b2f00", "label": "🟡 YELLOW – Urgent",
               "gradient": "linear-gradient(135deg,#fde68a,#eab308)"},
    "GREEN": {"bg": "#2e9d4f", "soft": "#e8f6ec", "fg": "#ffffff", "label": "🟢 GREEN – Routine",
              "gradient": "linear-gradient(135deg,#86efac,#16a34a)"},
}

# A different header gradient per page keeps each page recognisable.
HERO_GRADIENTS = {
    "home": "linear-gradient(120deg,#0d9488 0%,#2563eb 55%,#7c3aed 100%)",
    "intake": "linear-gradient(120deg,#7c3aed 0%,#c026d3 55%,#db2777 100%)",
    "dashboard": "linear-gradient(120deg,#0f766e 0%,#0891b2 50%,#2563eb 100%)",
    "audit": "linear-gradient(120deg,#4338ca 0%,#2563eb 50%,#0891b2 100%)",
    "alerts": "linear-gradient(120deg,#be123c 0%,#e11d48 50%,#f97316 100%)",
    "records": "linear-gradient(120deg,#334155 0%,#4338ca 60%,#7c3aed 100%)",
}

# Gradients for stat tiles that are not an urgency colour.
TILE_GRADIENTS = {
    "blue": "linear-gradient(135deg,#60a5fa,#4f46e5)",
    "teal": "linear-gradient(135deg,#5eead4,#0d9488)",
    "violet": "linear-gradient(135deg,#c084fc,#7c3aed)",
    "pink": "linear-gradient(135deg,#f9a8d4,#db2777)",
    "slate": "linear-gradient(135deg,#94a3b8,#475569)",
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@500;600;700&family=Inter:wght@400;500;600&display=swap');

html, body, .stApp, .stMarkdown, button, input, textarea { font-family: 'Inter', sans-serif; }
h1, h2, h3, .cq-hero h1 { font-family: 'Poppins', sans-serif !important; }

/* Page background: soft colour glows instead of flat grey */
.stApp {
  background:
    radial-gradient(circle at 8% 0%, #ccfbf1 0, transparent 32%),
    radial-gradient(circle at 92% 6%, #ede9fe 0, transparent 30%),
    radial-gradient(circle at 50% 100%, #e0f2fe 0, transparent 40%),
    #f4f6fb;
}
.block-container { padding-top: 2.2rem; max-width: 1200px; }

/* Sidebar */
[data-testid="stSidebar"] { background: linear-gradient(180deg,#0f766e 0%,#1e3a8a 100%); }
[data-testid="stSidebar"] * { color: #f1f5f9 !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] { background: rgba(255,255,255,.08); border: none; }
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
  background: rgba(255,255,255,.14) !important; border: 1px solid rgba(255,255,255,.45) !important; }
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover { background: rgba(255,255,255,.26) !important; }
.cq-brand { display:flex; align-items:center; gap:.6rem; padding:.3rem 0 1rem; }
.cq-brand-logo { font-size:1.8rem; background:rgba(255,255,255,.15); border-radius:12px; padding:.2rem .5rem; }
.cq-brand-name { font-family:'Poppins',sans-serif; font-weight:700; font-size:1.35rem; letter-spacing:.5px; }
.cq-provider { display:inline-block; background:rgba(255,255,255,.18); border-radius:999px;
               padding:.15rem .7rem; font-size:.8rem; font-weight:600; }

/* Gradient page header */
.cq-hero { position:relative; overflow:hidden; border-radius:20px; padding:1.6rem 1.8rem; color:#fff;
           display:flex; align-items:center; gap:1.2rem; margin-bottom:1rem;
           box-shadow:0 10px 30px rgba(37,99,235,.25); }
.cq-hero::after { content:""; position:absolute; right:-60px; top:-60px; width:220px; height:220px;
                  border-radius:50%; background:rgba(255,255,255,.12); }
.cq-hero::before { content:""; position:absolute; right:90px; bottom:-80px; width:160px; height:160px;
                   border-radius:50%; background:rgba(255,255,255,.08); }
.cq-hero-icon { font-size:2.6rem; background:rgba(255,255,255,.18); border-radius:16px; padding:.4rem .8rem; }
.cq-hero h1 { color:#fff !important; margin:0; padding:0; font-size:2rem; }
.cq-hero p { margin:.2rem 0 0; opacity:.92; font-size:1.02rem; }

/* Disclaimer banner */
.cq-disclaimer { background:linear-gradient(90deg,#fff7ed,#fef3c7); border-left:6px solid #f59e0b;
                 border-radius:12px; padding:.7rem 1rem; color:#7c2d12; font-weight:500;
                 margin-bottom:1rem; box-shadow:0 2px 8px rgba(245,158,11,.15); }

/* Stat tiles */
.cq-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr)); gap:.8rem; margin:.4rem 0 1rem; }
.cq-tile { border-radius:16px; padding:.9rem 1rem; color:#fff; box-shadow:0 6px 18px rgba(15,23,42,.12);
           position:relative; overflow:hidden; }
.cq-tile::after { content:""; position:absolute; right:-25px; bottom:-25px; width:80px; height:80px;
                  border-radius:50%; background:rgba(255,255,255,.18); }
.cq-tile-icon { font-size:1.3rem; }
.cq-tile-value { font-family:'Poppins',sans-serif; font-size:1.9rem; font-weight:700; line-height:1.1; }
.cq-tile-label { font-size:.82rem; font-weight:600; opacity:.95; text-transform:uppercase; letter-spacing:.4px; }

/* Generic white card */
.cq-card { background:#fff; border-radius:16px; padding:1.1rem 1.2rem; box-shadow:0 4px 16px rgba(15,23,42,.07);
           border-top:5px solid var(--cq-accent,#0d9488); height:100%; }
.cq-card h4 { margin:.5rem 0 .35rem; padding:0 !important; font-size:1.15rem; font-family:'Poppins',sans-serif; }
.cq-card p { margin:0; color:#475569; font-size:.92rem; }
.cq-step { display:inline-flex; align-items:center; justify-content:center; width:2.1rem; height:2.1rem;
           border-radius:50%; color:#fff; font-weight:700; background:var(--cq-accent,#0d9488); }
.cq-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:1rem; margin:.5rem 0 1.2rem; }

/* Patient cards (Streamlit containers with key "card_<URGENCY>_<id>") */
[class*="st-key-card_"] { background:#fff; border-radius:18px; padding:1rem 1.2rem 1.1rem;
                          box-shadow:0 6px 20px rgba(15,23,42,.08); border-left:10px solid #94a3b8;
                          margin-bottom:.6rem; }
[class*="st-key-card_RED"] { border-left-color:#e53935; background:linear-gradient(90deg,#fff5f5,#fff 35%); }
[class*="st-key-card_ORANGE"] { border-left-color:#f57c00; background:linear-gradient(90deg,#fff8f0,#fff 35%); }
[class*="st-key-card_YELLOW"] { border-left-color:#f9c80e; background:linear-gradient(90deg,#fffdf0,#fff 35%); }
[class*="st-key-card_GREEN"] { border-left-color:#2e9d4f; background:linear-gradient(90deg,#f3fbf5,#fff 35%); }
[class*="st-key-panel"] { background:#fff; border-radius:18px; padding:1.1rem 1.3rem;
                          box-shadow:0 6px 20px rgba(15,23,42,.07); }

.cq-head { display:flex; align-items:center; gap:.9rem; flex-wrap:wrap; }
.cq-qnum { min-width:3.2rem; height:3.2rem; border-radius:14px; display:flex; align-items:center;
           justify-content:center; font-family:'Poppins',sans-serif; font-weight:700; font-size:1.25rem; }
.cq-who { flex:1; min-width:180px; }
.cq-name { font-family:'Poppins',sans-serif; font-weight:600; font-size:1.15rem; color:#0f172a; }
.cq-meta { color:#64748b; font-size:.88rem; }
.cq-complaint { font-size:1.08rem; font-weight:600; color:#0f172a; margin:.7rem 0 .4rem; }
.cq-complaint span { color:#64748b; font-weight:500; font-size:.85rem; text-transform:uppercase; letter-spacing:.4px; }
.cq-chips { display:flex; gap:.4rem; flex-wrap:wrap; margin:.3rem 0 .5rem; }
.cq-note { background:#fffbeb; border-left:5px solid #f59e0b; border-radius:10px; padding:.6rem .8rem;
           color:#78350f; font-size:.95rem; margin:.4rem 0 .5rem; }
.cq-why { color:#475569; font-size:.88rem; background:#f8fafc; border-radius:10px; padding:.5rem .7rem; }

/* Badges / chips */
.cq-pill { display:inline-block; padding:.22rem .75rem; border-radius:999px; font-weight:700;
           font-size:.8rem; letter-spacing:.3px; white-space:nowrap; }
.cq-chip { display:inline-block; padding:.2rem .65rem; border-radius:999px; font-size:.8rem;
           font-weight:600; white-space:nowrap; }
.cq-chip-blue { background:#dbeafe; color:#1e40af; }
.cq-chip-violet { background:#ede9fe; color:#5b21b6; }
.cq-chip-teal { background:#ccfbf1; color:#115e59; }
.cq-chip-amber { background:#fef3c7; color:#92400e; }
.cq-chip-slate { background:#e2e8f0; color:#334155; }
.cq-chip-pink { background:#fce7f3; color:#9d174d; }
.cq-overdue { background:#e11d48; color:#fff; animation:cq-pulse 1.6s ease-in-out infinite; }
@keyframes cq-pulse { 0%,100% { box-shadow:0 0 0 0 rgba(225,29,72,.55); } 50% { box-shadow:0 0 0 7px rgba(225,29,72,0); } }

/* Colour key */
.cq-legend { background:#fff; border-radius:12px; padding:.5rem .8rem; margin:.3rem 0 .4rem;
             box-shadow:0 2px 8px rgba(15,23,42,.06); font-size:.9rem; color:#334155; line-height:2; }
.cq-swatch { display:inline-block; width:22px; height:8px; border-radius:999px; vertical-align:middle; }

/* Wait-time progress bar */
.cq-wait { font-size:.88rem; color:#334155; margin:.4rem 0 .5rem; }
.cq-bar { height:8px; border-radius:999px; background:#e2e8f0; overflow:hidden; margin-top:.3rem; }
.cq-bar > div { height:100%; border-radius:999px; }

/* Simple table (Seen today) */
.cq-table { width:100%; border-collapse:collapse; font-size:.9rem; }
.cq-table th { text-align:left; color:#64748b; font-weight:600; font-size:.78rem; text-transform:uppercase;
               letter-spacing:.4px; padding:.45rem .5rem; border-bottom:2px solid #e2e8f0; }
.cq-table td { padding:.5rem; border-bottom:1px solid #f1f5f9; }
.cq-table tr:hover td { background:#f8fafc; }

/* Segmented controls (e.g. Audit Log filter) */
[data-testid="stButtonGroup"] button { font-weight:600; padding:.45rem 1rem; }

/* Ticket shown to the patient after intake */
.cq-ticket { border-radius:20px; padding:1.4rem 1.6rem; color:#fff; display:flex; align-items:center; gap:1.4rem;
             background:linear-gradient(120deg,#0d9488,#2563eb); box-shadow:0 10px 30px rgba(13,148,136,.3); }
.cq-ticket-num { font-family:'Poppins',sans-serif; font-size:3.2rem; font-weight:700; line-height:1;
                 background:rgba(255,255,255,.18); border-radius:16px; padding:.6rem 1.1rem; }
.cq-ticket p { margin:.2rem 0 0; opacity:.95; }

.cq-section { font-family:'Poppins',sans-serif; font-weight:600; font-size:1.25rem; color:#0f172a;
              margin:1.2rem 0 .3rem; display:flex; align-items:center; gap:.5rem; }
.cq-section small { font-family:'Inter',sans-serif; font-weight:500; font-size:.85rem; color:#64748b; }

/* Streamlit widgets */
[data-testid="stForm"] { background:#fff; border-radius:18px; border:none; padding:1.4rem;
                         box-shadow:0 6px 20px rgba(15,23,42,.07); }
[data-testid="stExpander"] { border-radius:12px; border:1px solid #e2e8f0; background:#fff; }
.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] > button {
  border-radius:10px; font-weight:600; transition:transform .12s ease, box-shadow .12s ease; }
.stButton > button:hover, .stDownloadButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {
  transform:translateY(-1px); box-shadow:0 6px 14px rgba(37,99,235,.2); }
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background:linear-gradient(90deg,#0d9488,#2563eb) !important; border:none !important; color:#fff !important; }
[data-testid="stDataFrame"] { border-radius:12px; overflow:hidden; box-shadow:0 4px 14px rgba(15,23,42,.06); }
</style>
"""


def page_setup(title: str, icon: str = "🏥") -> None:
    """Call at the top of every page: config, theme CSS, sidebar branding and the disclaimer."""
    st.set_page_config(page_title=f"ClinicQ – {title}", page_icon=icon, layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    provider = get_provider_name()
    st.sidebar.markdown(
        "<div class='cq-brand'><span class='cq-brand-logo'>🏥</span>"
        "<span class='cq-brand-name'>ClinicQ</span></div>"
        f"<span class='cq-provider'>🤖 AI: {escape(provider)}</span>",
        unsafe_allow_html=True,
    )
    if provider == "mock":
        st.sidebar.caption("Mock mode: offline keyword logic, no API key needed.")
    open_alerts = count_open_alerts()
    if open_alerts:
        st.sidebar.markdown(f"<span class='cq-chip cq-overdue'>🚨 {open_alerts} open alert(s)</span>",
                            unsafe_allow_html=True)
    st.sidebar.caption("⚠️ " + DISCLAIMER)

    st.markdown(f"<div class='cq-disclaimer'>⚠️ {DISCLAIMER}</div>", unsafe_allow_html=True)


def hero(title: str, subtitle: str, icon: str, page: str) -> None:
    """Big gradient header at the top of a page."""
    st.markdown(
        f"<div class='cq-hero' style='background:{HERO_GRADIENTS[page]}'>"
        f"<div class='cq-hero-icon'>{icon}</div>"
        f"<div><h1>{escape(title)}</h1><p>{escape(subtitle)}</p></div></div>",
        unsafe_allow_html=True,
    )


def tile(label: str, value, icon: str, colour: str) -> str:
    """HTML for one stat tile. colour = an urgency level or a TILE_GRADIENTS key."""
    if colour in URGENCY_STYLE:
        gradient, fg = URGENCY_STYLE[colour]["gradient"], URGENCY_STYLE[colour]["fg"]
    else:
        gradient, fg = TILE_GRADIENTS[colour], "#ffffff"
    return (f"<div class='cq-tile' style='background:{gradient};color:{fg}'>"
            f"<div class='cq-tile-icon'>{icon}</div>"
            f"<div class='cq-tile-value'>{escape(str(value))}</div>"
            f"<div class='cq-tile-label'>{escape(label)}</div></div>")


def tiles(items: list[str]) -> None:
    """Show a responsive row of tiles built with tile()."""
    st.markdown(f"<div class='cq-tiles'>{''.join(items)}</div>", unsafe_allow_html=True)


def section(title: str, note: str = "") -> None:
    small = f"<small>{escape(note)}</small>" if note else ""
    st.markdown(f"<div class='cq-section'>{title} {small}</div>", unsafe_allow_html=True)


def urgency_badge(urgency: str) -> str:
    """HTML for a coloured urgency pill."""
    style = URGENCY_STYLE[urgency]
    return f"<span class='cq-pill' style='background:{style['bg']};color:{style['fg']}'>{urgency}</span>"


def chip(text: str, colour: str = "slate", extra_class: str = "") -> str:
    """HTML for a small rounded label. colour: blue, violet, teal, amber, slate, pink."""
    return f"<span class='cq-chip cq-chip-{colour} {extra_class}'>{escape(text)}</span>"


# ---------------------------------------------------------------------------
# Colour key – what the triage colours mean
# ---------------------------------------------------------------------------
URGENCY_MEANING = {
    "RED": ("Emergency", "Life may be in danger",
            "chest pain, difficulty breathing, unconscious, seizure, heavy bleeding, pregnancy danger signs, "
            "a child who is not drinking"),
    "ORANGE": ("Very urgent", "Could become dangerous soon",
               "high fever, burns, severe pain, head injury, coughing blood, very high blood pressure"),
    "YELLOW": ("Urgent", "Needs to be seen today",
               "cough for many days, moderate pain, AI could not read the entry (manual review)"),
    "GREEN": ("Routine", "Not urgent",
              "repeat medication, mild cold, check-up"),
}


def _target_text(level: str) -> str:
    from core.models import Urgency
    from core.queue_manager import TARGET_MINUTES  # imported here to avoid a circular import
    minutes = TARGET_MINUTES[Urgency(level)]
    if minutes == 0:
        return "See immediately"
    return f"See within {minutes // 60} hour{'s' if minutes >= 120 else ''}" if minutes >= 60 \
        else f"See within {minutes} min"


def colour_key(full: bool = True) -> None:
    """Explain the triage colours. full=True: cards with examples (Home). False: a compact strip (Dashboard)."""
    if full:
        cards = ""
        for level, (name, meaning, examples) in URGENCY_MEANING.items():
            style = URGENCY_STYLE[level]
            cards += (
                f"<div class='cq-card' style='--cq-accent:{style['bg']};background:{style['soft']}'>"
                f"{urgency_badge(level)}<h4>{name}</h4>"
                f"<p><b>{_target_text(level)}</b> · {meaning}</p>"
                f"<p style='margin-top:.35rem'>e.g. {examples}</p></div>"
            )
        st.markdown(f"<div class='cq-grid'>{cards}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='cq-chips'>{chip('⏰ OVERDUE', extra_class='cq-overdue')} "
            "waited longer than the target time for their colour</div>"
            f"<div class='cq-chips'>{chip('⬆️ Moved up for waiting', 'pink')} "
            "a GREEN patient waiting over 4 h now queues with YELLOW patients</div>"
            "<div class='cq-chips' style='color:#475569;font-size:.88rem'>Colours come from the red-flag "
            "rules and the AI, can be raised by vital signs, and are always confirmed by the vitals nurse. "
            "Within each colour, whoever arrived first is seen first.</div>",
            unsafe_allow_html=True,
        )
        return

    strip = " &nbsp; ".join(
        f"{urgency_badge(level)} <b>{name}</b> – {_target_text(level).lower()}"
        for level, (name, _, _) in URGENCY_MEANING.items()
    )
    st.markdown(
        f"<div class='cq-legend'>{strip}</div>"
        "<div class='cq-legend' style='font-size:.82rem'>Waiting bar: "
        "<span class='cq-swatch' style='background:#0d9488'></span> within target &nbsp; "
        "<span class='cq-swatch' style='background:#f59e0b'></span> over half the target &nbsp; "
        "<span class='cq-swatch' style='background:#e11d48'></span> overdue</div>",
        unsafe_allow_html=True,
    )
    with st.expander("🎨 What do the colours mean? (examples)"):
        for level, (name, meaning, examples) in URGENCY_MEANING.items():
            st.markdown(f"{urgency_badge(level)} **{name}** – {_target_text(level)}. {meaning}. "
                        f"*e.g. {examples}.*", unsafe_allow_html=True)
        st.markdown("⏰ **Overdue** = waited longer than the target for their colour. "
                    "⬆️ **Moved up** = GREEN patient waiting over 4 h now queues with YELLOW.")
