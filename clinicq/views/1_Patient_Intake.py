"""Patient Intake (registration): personal details, next of kin, and symptoms in any of five languages."""

from datetime import date, datetime, timedelta
from html import escape

import streamlit as st
from pydantic import ValidationError

from core import database as db
from core.llm_client import BrokenMockClient
from core.models import CHRONIC_CONDITIONS, ID_TYPES, PatientIntake
from core.queue_manager import notify_near_front
from core.sms_service import get_sms_service
from core.triage import run_triage
from core.ui import URGENCY_STYLE, chip, hero, page_setup, section, urgency_badge

# Staff can back-date arrival by at most this much (catches typing mistakes).
MAX_BACKDATE = timedelta(hours=12)

page_setup("Patient Intake", "📝")
hero("Patient Intake", "Tell us how you are feeling – in your own words and your own language", "📝", "intake")

languages = [("English", "blue"), ("isiZulu", "violet"), ("Sesotho", "teal"),
             ("Sepedi", "pink"), ("Afrikaans", "amber")]
st.markdown(
    "<div class='cq-chips'>🗣️ &nbsp;" + "".join(chip(name, colour) for name, colour in languages)
    + "</div>",
    unsafe_allow_html=True,
)
st.caption("🔒 Your details are kept by the clinic for your care. They are never sent to the AI, and staff screens only show part of your ID and phone number.")

with st.form("intake_form", clear_on_submit=False):
    section("👤 Your details")
    col1, col2 = st.columns(2)
    first_name = col1.text_input("First name *", max_chars=40)
    surname = col2.text_input("Surname *", max_chars=40)
    col3, col4 = st.columns([1, 2])
    id_type = col3.selectbox("ID document *", ID_TYPES,
                             help="Everyone can be helped at the clinic, with or without an SA ID.")
    id_number = col4.text_input("ID / passport / permit number",
                                placeholder="13-digit SA ID, or passport/permit number")
    col5, col6, col7 = st.columns(3)
    age = col5.number_input("Age *", min_value=0, max_value=120, value=30, step=1)
    sex = col6.selectbox("Sex *", ["Female", "Male", "Other"])
    phone = col7.text_input("📱 Contact number (for SMS)", placeholder="0821234567",
                            help="Optional if you do not have a phone.")
    address = st.text_input("🏠 Home address *", placeholder="Street, area / township")

    section("👪 Next of kin")
    col8, col9, col10 = st.columns(3)
    nok_name = col8.text_input("Full name *", key="nok_name")
    nok_relationship = col9.text_input("Relationship *", placeholder="e.g. Mother, Husband, Friend")
    nok_phone = col10.text_input("Phone number *", placeholder="0821234567", key="nok_phone")

    section("🩺 Your health")
    col11, col12 = st.columns([1, 2])
    pregnant = col11.radio("Pregnant?", ["N/A", "No", "Yes"], horizontal=True)
    conditions = col12.multiselect("Known chronic conditions", CHRONIC_CONDITIONS)

    section("💬 How are you feeling?")
    symptoms_text = st.text_area(
        "Describe how you are feeling *",
        height=140,
        placeholder="English, isiZulu, Sesotho, Sepedi or Afrikaans – e.g. 'Isifuba sibuhlungu…'",
    )

    consent = st.checkbox(
        "I agree that the clinic may store my details and health information for my care, "
        "and share them with a hospital if I am referred. *"
    )

    with st.expander("🕘 For staff: arrival time & demo options"):
        arrived_at = st.time_input(
            "Arrived earlier than now? Enter the arrival time (optional)",
            value=None,
            step=300,
            help="Leave empty to use the current time. Use this when the patient queued "
                 "outside before registering, so their waiting time is fair.",
        )
        simulate_failure = st.checkbox("Simulate AI failure (shows the safe YELLOW fallback)", value=False)

    submitted = st.form_submit_button("Submit ➜", type="primary", width="stretch")

if submitted:
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
                  "symptoms_text": "How you are feeling"}
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

    # --- Triage and save ---------------------------------------------------
    with st.spinner("Checking danger signs and structuring the information…"):
        client = BrokenMockClient() if simulate_failure else None
        triage = run_triage(intake, client=client)
        patient_id = db.add_patient(intake, triage, arrival_time=arrival_iso,
                                    arrival_entered_by_staff=arrival_iso is not None)
        notify_near_front(get_sms_service())

    patient = db.get_patient(patient_id)

    # --- What the patient sees ---------------------------------------------
    st.markdown(
        f"<div class='cq-ticket'><div class='cq-ticket-num'>#{patient['queue_number']}</div>"
        f"<div><h3 style='color:#fff;margin:0'>Thank you, {escape(intake.first_name)}!</h3>"
        "<p>This is your queue number. Please wait to be called for <b>vitals & tests</b> "
        "(blood pressure, temperature, urine).</p>"
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
                f"“{escape(m.matched_phrase)}” <span class='cq-chip cq-chip-slate'>{m.language}</span>",
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
