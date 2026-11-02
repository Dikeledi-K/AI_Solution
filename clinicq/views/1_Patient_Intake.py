"""Patient Intake (registration at reception), done by a signed-in registration clerk.

1. Returning patient? Search by folder number, ID/passport number or name, and the
   form is filled in from their folder (check it with the patient and update it).
2. Personal details, next of kin and consent.
3. Symptoms in the patient's own words, in any of five languages.

Every visit is linked to the patient's clinic FOLDER, so earlier visits are always
found. A person who is already in the clinic cannot be registered twice, and an ID
number that already has a folder is linked to it automatically.
"""

import json
import time
from datetime import date, datetime, timedelta
from html import escape

import streamlit as st
from pydantic import ValidationError

from core import database as db
from core.auth import staff_label
from core.auth_ui import require_staff
from core.llm_client import BrokenMockClient
from core.models import CHRONIC_CONDITIONS, ID_TYPES, PatientIntake
from core.privacy import age_on, mask_id
from core.queue_manager import notify_near_front
from core.settings import demo_mode
from core.sms_service import get_sms_service
from core.triage import run_triage
from core.ui import URGENCY_STYLE, chip, hero, page_setup, section, urgency_badge

# Staff can back-date arrival by at most this much (catches typing mistakes).
MAX_BACKDATE = timedelta(hours=12)
# Stops a double-click (or a stuck key) from registering the same person twice.
MIN_SECONDS_BETWEEN_REGISTRATIONS = 5

page_setup("Patient Intake", "📝")
hero("Patient Intake", "Register a patient – symptoms in their own words and their own language", "📝",
     "intake")

account = require_staff("intake")
REGISTRAR = staff_label(account)

languages = [("English", "blue"), ("isiZulu", "violet"), ("Sesotho", "teal"),
             ("Sepedi", "pink"), ("Afrikaans", "amber")]
st.markdown(
    "<div class='cq-chips'>🗣️ &nbsp;" + "".join(chip(name, colour) for name, colour in languages)
    + "</div>",
    unsafe_allow_html=True,
)
st.caption("🔒 Personal details are kept by the clinic for the patient's care. They are never sent to the AI, "
           "and staff screens only show part of the ID and phone number.")

# ---------------------------------------------------------------------------
# 1. Returning patient: find their folder
# ---------------------------------------------------------------------------
prefill = st.session_state.get("intake_prefill")

if not prefill:
    section("🔎 Returning patient?", "search first, so nobody gets a second folder")
    query = st.text_input("Folder number, ID/passport number or name",
                          placeholder="e.g. F000012, 8501015800087 or Mokoena", key="lookup_query")
    if query.strip():
        found = db.find_returning_patients(query)
        if st.session_state.get("lookup_logged") != query:  # log each search once (POPIA accountability)
            db.add_audit("RECORD_LOOKUP", nurse_action=f"Folder search by {REGISTRAR}",
                         details=f"{len(found)} folder(s) found")
            st.session_state["lookup_logged"] = query
        if not found:
            st.info("No folder found. Register them as a new patient below.")
        else:
            options = {
                r["folder_number"]: (f"{r['folder_number']} · {r['first_name']} {r.get('surname') or ''} · "
                                     f"{r['age']}y {r['sex']} · ID {mask_id(r.get('id_number'))} · "
                                     f"last visit {r['arrival_time'][:10]}")
                for r in found
            }
            c1, c2 = st.columns([3, 1])
            chosen = c1.selectbox("Folders found", list(options), format_func=options.get)
            if c2.button("Use this folder ➜", type="primary", width="stretch"):
                row = next(r for r in found if r["folder_number"] == chosen)
                db.add_audit("RECORD_LOOKUP", patient_id=row["id"],
                             nurse_action=f"Folder {chosen} opened for a new visit by {REGISTRAR}")
                st.session_state["intake_prefill"] = row
                st.rerun()
else:
    active = db.active_visit(prefill["folder_number"])
    st.markdown(
        f"<div class='cq-card' style='--cq-accent:#0d9488'>📁 <b>Returning patient – folder "
        f"{escape(prefill['folder_number'])}</b>: {escape(prefill['first_name'])} "
        f"{escape(prefill.get('surname') or '')}. Their details are filled in below – <b>check each one with "
        "the patient</b> and update anything that changed (phone, address, next of kin).</div>",
        unsafe_allow_html=True,
    )
    if active:
        st.error(f"This patient is already in the clinic today as queue number #{active['queue_number']} "
                 "and cannot be registered again.")
    if st.button("✖ Not this patient – register someone else"):
        st.session_state.pop("intake_prefill", None)
        st.rerun()


def _default_age(row: dict) -> int:
    """A returning patient's age today (from their date of birth when known)."""
    if row.get("date_of_birth"):
        return age_on(date.fromisoformat(row["date_of_birth"]))
    return int(row["age"])


pf = prefill or {}
# A new form (fresh widgets and defaults) for each folder and after each registration,
# so the next patient never starts with the previous patient's details.
form_key = f"{pf.get('folder_number', 'new')}_{st.session_state.get('intake_round', 0)}"

# ---------------------------------------------------------------------------
# 2. The registration form
# ---------------------------------------------------------------------------
with st.form(f"intake_form_{form_key}", clear_on_submit=False):
    section("👤 Personal details", "new patient" if not prefill else f"folder {pf['folder_number']}")
    col1, col2 = st.columns(2)
    first_name = col1.text_input("First name *", max_chars=40, value=pf.get("first_name", ""))
    surname = col2.text_input("Surname *", max_chars=40, value=pf.get("surname") or "")
    col3, col4 = st.columns([1, 2])
    id_type = col3.selectbox("ID document *", ID_TYPES,
                             index=ID_TYPES.index(pf["id_type"]) if pf.get("id_type") in ID_TYPES else 0,
                             help="Everyone can be helped at the clinic, with or without an SA ID.")
    id_number = col4.text_input("ID / passport / permit number", value=pf.get("id_number") or "",
                                placeholder="13-digit SA ID, or passport/permit number")
    col5, col6, col7 = st.columns(3)
    age = col5.number_input("Age *", min_value=0, max_value=120, value=_default_age(pf) if pf else 30, step=1)
    sexes = ["Female", "Male", "Other"]
    sex = col6.selectbox("Sex *", sexes, index=sexes.index(pf["sex"]) if pf.get("sex") in sexes else 0)
    phone = col7.text_input("📱 Contact number (for SMS)", placeholder="0821234567", value=pf.get("phone") or "",
                            help="Optional if the patient has no phone. They can watch the waiting-room screen.")
    address = st.text_input("🏠 Home address *", placeholder="Street, area / township", value=pf.get("address") or "")

    section("👪 Next of kin")
    col8, col9, col10 = st.columns(3)
    nok_name = col8.text_input("Full name *", value=pf.get("nok_name") or "")
    nok_relationship = col9.text_input("Relationship *", placeholder="e.g. Mother, Husband, Friend",
                                       value=pf.get("nok_relationship") or "")
    nok_phone = col10.text_input("Phone number *", placeholder="0821234567", value=pf.get("nok_phone") or "")

    section("🩺 Health")
    col11, col12 = st.columns([1, 2])
    pregnant = col11.radio("Pregnant?", ["N/A", "No", "Yes"], horizontal=True)
    known = [c for c in json.loads(pf.get("chronic_conditions") or "[]") if c in CHRONIC_CONDITIONS]
    conditions = col12.multiselect("Known chronic conditions", CHRONIC_CONDITIONS, default=known)

    section("💬 How is the patient feeling?", "type what they say, in their own words and language")
    symptoms_text = st.text_area(
        "Symptoms in the patient's own words *",
        height=140,
        placeholder="English, isiZulu, Sesotho, Sepedi or Afrikaans – e.g. 'Isifuba sibuhlungu…'",
    )

    consent = st.checkbox(
        "The patient agrees that the clinic may store their details and health information for their care, "
        "and share them with a hospital if they are referred. *"
    )

    with st.expander("🕘 Arrival time" + (" & demo options" if demo_mode() else "")):
        arrived_at = st.time_input(
            "Arrived earlier than now? Enter the arrival time (optional)",
            value=None,
            step=300,
            help="Leave empty to use the current time. Use this when the patient queued "
                 "outside before registering, so their waiting time is fair.",
        )
        simulate_failure = (st.checkbox("Simulate AI failure (shows the safe YELLOW fallback)", value=False)
                            if demo_mode() else False)

    submitted = st.form_submit_button("Register ➜", type="primary", width="stretch")

if submitted:
    last = st.session_state.get("last_registration", 0.0)
    if time.monotonic() - last < MIN_SECONDS_BETWEEN_REGISTRATIONS:
        st.warning("Please wait a moment – the last registration was only just saved.")
        st.stop()

    # --- Validate the form -------------------------------------------------
    try:
        intake = PatientIntake(
            first_name=first_name,
            surname=surname,
            id_type=id_type,
            id_number=id_number or None,
            address=address,
            nok_name=nok_name,
            nok_relationship=nok_relationship,
            nok_phone=nok_phone,
            consent=consent,
            age=int(age),
            sex=sex,
            pregnant=pregnant,
            chronic_conditions=conditions,
            phone=phone or None,
            symptoms_text=symptoms_text,
        )
    except ValidationError as e:
        labels = {"first_name": "First name", "surname": "Surname", "address": "Home address",
                  "nok_name": "Next of kin name", "nok_relationship": "Next of kin relationship",
                  "nok_phone": "Next of kin phone", "consent": "Consent", "phone": "Contact number",
                  "symptoms_text": "Symptoms"}
        for err in e.errors():
            field = labels.get(err["loc"][0], err["loc"][0]) if err["loc"] else "Details"
            st.error(f"**{field}**: {err['msg'].removeprefix('Value error, ')}")
        st.stop()

    arrival_iso = None
    if arrived_at is not None:
        now = datetime.now()
        arrival = datetime.combine(date.today(), arrived_at)
        if arrival > now:
            st.error("**Arrival time** cannot be in the future.")
            st.stop()
        if now - arrival > MAX_BACKDATE:
            st.error("**Arrival time** must be within the last 12 hours.")
            st.stop()
        arrival_iso = arrival.isoformat(timespec="seconds")

    # --- Which folder? (checked BEFORE the AI is called) --------------------
    folder = prefill["folder_number"] if prefill else None
    linked_by_id = False
    if folder is None and intake.id_number:
        folder = db.folder_for_id(intake.id_number)
        linked_by_id = folder is not None
    if folder and (active := db.active_visit(folder)):
        st.error(f"Folder {folder} ({active['first_name']} {active.get('surname') or ''}) is already in the "
                 f"clinic today as queue number #{active['queue_number']}. They cannot be registered twice.")
        st.stop()

    # --- Triage and save ---------------------------------------------------
    with st.spinner("Checking danger signs and structuring the information…"):
        client = BrokenMockClient() if simulate_failure else None
        triage = run_triage(intake, client=client)
        try:
            patient_id = db.add_patient(intake, triage, arrival_time=arrival_iso,
                                        arrival_entered_by_staff=arrival_iso is not None,
                                        folder_number=folder, registered_by=REGISTRAR)
        except db.AlreadyInClinic as e:  # registered at another desk a moment ago
            st.error(str(e))
            st.stop()
        notify_near_front(get_sms_service())
    st.session_state["last_registration"] = time.monotonic()
    st.session_state["intake_round"] = st.session_state.get("intake_round", 0) + 1
    st.session_state.pop("intake_prefill", None)

    patient = db.get_patient(patient_id)
    if linked_by_id:
        st.info(f"This ID number already had folder **{patient['folder_number']}**, so the visit was added to "
                "that folder.")

    # --- The ticket ----------------------------------------------------------
    st.markdown(
        f"<div class='cq-ticket'><div class='cq-ticket-num'>#{patient['queue_number']}</div>"
        f"<div><h3 style='color:#fff;margin:0'>Thank you, {escape(intake.first_name)}!</h3>"
        f"<p>Folder <b>{escape(patient['folder_number'])}</b>. This is your queue number. Please wait to be "
        "called for <b>vitals & tests</b> – watch the waiting-room screen"
        + (" or your phone" if intake.phone else "") + ".</p>"
        "<p><b>If you feel worse while waiting, tell the nurse immediately.</b></p></div></div>",
        unsafe_allow_html=True,
    )

    # --- What staff see ----------------------------------------------------
    final = triage.final_urgency.value
    style = URGENCY_STYLE[final]
    section("🩺 Staff view", "suggestion only – needs nurse confirmation")
    with st.container(key=f"card_{final}_new"):
        st.markdown(
            f"<div class='cq-head'><span class='cq-pill' style='background:{style['bg']};"
            f"color:{style['fg']};font-size:.95rem'>{style['label']}</span>"
            f"{chip('decided by ' + triage.decided_by, 'violet')}"
            f"{chip('arrival entered by staff', 'amber') if arrival_iso else ''}</div>",
            unsafe_allow_html=True,
        )
        for m in triage.rules_result.matches:
            st.markdown(
                f"🚩 {urgency_badge(m.urgency.value)} **{escape(m.rule_name)}** – matched "
                f"“{escape(m.matched_phrase)}” <span class='cq-chip cq-chip-slate'>{escape(m.language)}</span>",
                unsafe_allow_html=True,
            )
        for m in triage.rules_result.negated:
            st.markdown(
                f"⚪ <b>{escape(m.rule_name)}</b> – “{escape(m.matched_phrase)}” was mentioned with "
                "<i>no / not / without</i>, so it did not raise the colour. The nurse will check.",
                unsafe_allow_html=True,
            )
        if triage.llm_output:
            out = triage.llm_output
            st.markdown(
                f"<div class='cq-complaint'><span>Chief complaint</span><br>{escape(out.chief_complaint)}</div>"
                f"<div class='cq-chips'>{chip('🗣️ ' + out.detected_language, 'blue')}"
                + "".join(chip(s, "teal") for s in out.symptoms)
                + f"</div><div class='cq-why'>🤖 {escape(out.reasoning)}<br>"
                f"<b>English:</b> {escape(out.english_translation)}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.error(f"AI could not structure this entry – set to YELLOW for manual review. ({triage.llm_error})")
        st.caption("The vitals nurse confirms or changes this colour on the Staff Dashboard.")
