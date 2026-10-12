"""ClinicQ – home page (shown first; see app.py for the page list)."""

import streamlit as st

from core import database as db
from core import rooms
from core.demo import load_demo_patients
from core.queue_manager import queue_stats
from core.stations import STATIONS
from core.sms_service import get_sms_service
from core.auth import staff_label
from core.auth_ui import current_staff
from core.ui import colour_key, hero, page_setup, section, tile, tiles

page_setup("Home")
db.init_db()

hero(
    "ClinicQ",
    "AI-assisted intake, triage and patient flow for South African public clinics",
    "🏥",
    "home",
)

# --- Live snapshot ------------------------------------------------------------
stats = queue_stats()
section("📊 Right now in the clinic")
tiles([tile("Waiting", stats["waiting_total"], "👥", "blue")]
      + [tile(STATIONS[s]["label"], stats["per_station"][s], STATIONS[s]["icon"], "teal") for s in STATIONS])
tiles([
    tile("Red · Emergency", stats["per_colour"]["RED"], "🚨", "RED"),
    tile("Orange · Very urgent", stats["per_colour"]["ORANGE"], "⚡", "ORANGE"),
    tile("Yellow · Urgent", stats["per_colour"]["YELLOW"], "⏳", "YELLOW"),
    tile("Green · Routine", stats["per_colour"]["GREEN"], "🌿", "GREEN"),
    tile("Overdue", stats["overdue"], "⏰", "pink"),
    tile("Open alerts", db.count_open_alerts(), "🚑", "violet"),
    tile("Rooms in use", f"{sum(1 for r in rooms.room_status() if r['claim'])}/{len(rooms.load_rooms())}",
         "🚪", "slate"),
])

# --- What the colours mean ------------------------------------------------------
section("🎨 What the colours mean", "every patient gets a triage colour that decides how soon they are seen")
colour_key(full=True)

# --- Problem ------------------------------------------------------------------
section("💡 The problem")
st.markdown(
    "<div class='cq-card' style='--cq-accent:#f59e0b'><p style='font-size:1rem'>"
    "Public clinic queues are long, and patients are often not assessed by urgency until late. "
    "Intake notes are handwritten, patients describe symptoms in <b>isiZulu, Sesotho, Sepedi, "
    "Afrikaans or English</b>, and staff in different rooms have no shared view of who is where. "
    "Someone with chest pain can end up waiting behind ten people collecting repeat medication.</p></div>",
    unsafe_allow_html=True,
)

# --- The patient journey -------------------------------------------------------
section("🧭 The patient journey")
steps = [
    ("#7c3aed", "📝", "Register",
     "Details, ID, next of kin, consent and symptoms in 5 languages. Red-flag rules, then AI, suggest a colour."),
    ("#0d9488", "🩺", "Vitals & tests",
     "BP, temperature, pulse, urine infection & pregnancy tests. Danger readings raise the colour. The nurse "
     "confirms it and leaves a note."),
    ("#2563eb", "👩‍⚕️", "Nurse / Doctor",
     "Reads the vitals note, prescribes, and ticks the rooms needed – or sends the patient home."),
    ("#db2777", "🧪", "HIV testing",
     "Counselling, consent and result. A positive result goes back to the Nurse/Doctor for ART."),
    ("#f57c00", "💉", "Injection room", "Gives the prescribed injection, then sends the patient on."),
    ("#16a34a", "💊", "Pharmacy", "Hands over the medication. The visit ends – 🏠 home."),
]
cards = "".join(
    f"<div class='cq-card' style='--cq-accent:{colour}'>"
    f"<span class='cq-step'>{i}</span> <span style='font-size:1.4rem'>{icon}</span>"
    f"<h4>{title}</h4><p>{text}</p></div>"
    for i, (colour, icon, title, text) in enumerate(steps, start=1)
)
st.markdown(f"<div class='cq-grid'>{cards}</div>", unsafe_allow_html=True)
st.caption("Steps 4–6 only happen if the Nurse/Doctor ticks them, and always in this order. Each room calls the "
           "patient, does its work and sends them to the next room.")

# --- What else ClinicQ does ---------------------------------------------------------
section("✨ What else ClinicQ does")
extras = [
    ("#e11d48", "🚑", "Emergencies",
     "Hospital referral alerts the ambulance coordinator, then the receiving hospital. A death alerts the "
     "facility manager, with tasks to certify, inform next of kin in person, and report to SAPS if needed."),
    ("#f57c00", "⏰", "Fair waiting",
     "Most urgent first. SATS target times flag overdue patients; GREEN patients waiting over 4 h move up."),
    ("#0891b2", "🚪", "Named rooms",
     "14 rooms – vitals, consultation, emergency, HIV counselling, injection, pharmacy. Staff pick a free room; "
     "one patient per room."),
    ("#4338ca", "🔐", "Secure sign-in",
     "Personal staff ID + PIN, roles, lockout after 5 wrong PINs and automatic sign-out after 30 min."),
    ("#be123c", "🔒", "Privacy",
     "No personal details go to the AI. IDs and phones are masked. HIV results are need-to-know only. SMS "
     "shows the room only."),
    ("#334155", "🗂️", "Records & audit",
     "Searchable patient records. Every decision, room, alert, sign-in and record view is logged with a name."),
]
cards = "".join(
    f"<div class='cq-card' style='--cq-accent:{colour}'><span style='font-size:1.4rem'>{icon}</span>"
    f"<h4>{title}</h4><p>{text}</p></div>"
    for colour, icon, title, text in extras
)
st.markdown(f"<div class='cq-grid'>{cards}</div>", unsafe_allow_html=True)

# --- Which page do I use? -------------------------------------------------------------
section("🧑‍🤝‍🧑 Which page do I use?")
guide = [
    ("📝 Patient Intake", "Patients / clerk", "Register and get a queue number. No sign-in."),
    ("🩺 Staff Dashboard", "Nurses, doctors, pharmacists, HIV counsellors",
     "Sign in, choose your station and a free room, call patients, record your part, send them on."),
    ("🚨 Alerts", "Ambulance coordinator, facility manager", "Ambulance, hospital and death follow-ups."),
    ("🗂️ Patient Records", "Nurses, doctors, facility manager, admin", "Search visits and full details."),
    ("📋 Audit Log", "Facility manager, admin", "Who did what, and when."),
    ("👥 Staff", "Everyone (own PIN) · admin", "Change your PIN; admins manage accounts and rooms."),
]
rows = "".join(f"<tr><td><b>{page}</b></td><td>{who}</td><td>{what}</td></tr>" for page, who, what in guide)
st.markdown("<div class='cq-card' style='--cq-accent:#7c3aed'><table class='cq-table'>"
            f"<tr><th>Page</th><th>Who</th><th>For</th></tr>{rows}</table></div>",
            unsafe_allow_html=True)

st.info("ClinicQ is a **decision-support** tool. It does not diagnose or prescribe – staff make every clinical decision. Personal details are never sent to the AI.")

# --- Demo controls ------------------------------------------------------------
section("🎬 Demo controls")
with st.container(key="panel_demo"):
    # A message saved before st.rerun() so it survives the refresh of the tiles above.
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    col1, col2 = st.columns(2)
    with col1:
        if st.button("👥 Load demo patients", type="primary", width="stretch"):
            with st.spinner("Triaging 10 demo patients…"):
                added = load_demo_patients(get_sms_service())
            st.session_state["flash"] = (f"Added {added} demo patients. They are waiting for "
                                         "**Vitals** – open the **Staff Dashboard** and sign in.")
            st.rerun()
    with col2:
        staff = current_staff()
        is_admin = bool(staff) and staff["role"] == "Admin"
        if st.button("🗑️ Reset all data", width="stretch", disabled=not is_admin,
                     help=None if is_admin else "Only a signed-in Admin can reset data."):
            db.clear_all()
            # Logged AFTER clearing, so the record of who reset the data is kept.
            db.add_audit("RESET", nurse_action=f"All patient data deleted by {staff_label(staff)}")
            sms = get_sms_service()
            if hasattr(sms, "clear"):
                sms.clear()
            st.session_state["flash"] = "Queue, audit log and SMS outbox cleared."
            st.rerun()
