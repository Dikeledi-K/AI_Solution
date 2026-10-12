"""Staff Dashboard – one page for every station in the clinic.

Each staff member signs in with their STAFF ID and PIN, then picks a STATION
(only those their role allows) and a ROOM. They see their
station's queue (most urgent first), call ONE patient at a time into their room,
and the "With you now" card shows exactly what that station needs to do:

    🩺 Vitals & tests   BP, temperature, pulse, urine tests -> confirm colour -> Nurse/Doctor
    👩‍⚕️ Nurse / Doctor   assessment & prescription -> Pharmacy / Injection / HIV testing / home
    💊 💉 🧪 stations    done -> next stop (or home), or back to the Nurse/Doctor

At Vitals and Nurse/Doctor, staff can also refer to hospital (alerts the
ambulance coordinator) or record a death (alerts the facility manager).
Any number of staff can use the page at once; it refreshes every 15 seconds.
"""

import json
from datetime import date, datetime
from html import escape

import streamlit as st
from pydantic import ValidationError
from streamlit.errors import StreamlitAPIException

from core import database as db
from core.demo import load_demo_patients
from core.models import Urgency, Vitals
from core.privacy import mask_id, mask_phone
from core.queue_manager import (TARGET_MINUTES, CallError, call_patient, effective_urgency,
                                get_waiting_queue, is_overdue, log_new_overdue,
                                minutes_over_target, moved_up_for_waiting, not_here,
                                queue_stats, wait_minutes)
from core.sms_service import MockSMSService, get_sms_service
from core.stations import AFTER_CLINICIAN, STATIONS, label
from core import rooms
from core.auth import allowed_stations, can_see_hiv_result, staff_label
from core.auth_ui import require_staff, session_expired
from core.ui import URGENCY_STYLE, chip, colour_key, hero, page_setup, tile, tiles, urgency_badge
from core.workflow import (HIV_RESULTS, complete_clinician, complete_station, load_contacts,
                           record_death, record_hiv_test, record_vitals, refer_to_hospital,
                           send_back_to_clinician)

AUTO_REFRESH_SECONDS = 15

page_setup("Staff Dashboard", "🩺")
db.init_db()
sms = get_sms_service()

hero("Staff Dashboard", "Sign in securely, call one patient at a time, and send them on.",
     "🩺", "dashboard")


# ---------------------------------------------------------------------------
# 1. Authenticate (staff ID + PIN), 2. choose a station allowed for the role + a room.
# Both live only in this browser session – never in the page URL.
# ---------------------------------------------------------------------------
account = require_staff("dashboard")
stations_allowed = allowed_stations(account["role"])
if not stations_allowed:
    st.info(f"Your role ({account['role']}) does not work at a station. "
            "Use the 🚨 Alerts page for your tasks.")
    st.stop()

shift = st.session_state.get("staff")
# Lost the room (e.g. inactive too long and someone else took it)? Choose again.
if shift and not rooms.heartbeat(account["staff_id"]):
    st.session_state.pop("staff", None)
    st.session_state["room_lost"] = shift["room"]
    shift = None

if not shift or shift["station"] not in stations_allowed:
    with st.container(key="panel_station"):
        st.markdown(f"#### 👋 Welcome, {escape(account['full_name'])} – where are you working?")
        if "room_lost" in st.session_state:
            st.warning(f"You are no longer in {st.session_state.pop('room_lost')} (no activity for "
                       f"{rooms.CLAIM_MINUTES} minutes). Please choose a room again.")
        st.caption("Only rooms for your station that are free right now are listed. "
                   "Each room can call one patient at a time.")
        c1, c2 = st.columns(2)
        station = c1.selectbox("Your station *", stations_allowed, format_func=label, key="pick_station")
        free = rooms.available_rooms(station, account["staff_id"])
        room = c2.selectbox("Your room *", free, index=None, key="pick_room",
                            placeholder="Choose a free room" if free else "No free room for this station")
        if not free:
            st.error(f"All rooms for {label(station)} are in use right now. Ask the facility manager or "
                     "an admin to free a room on the Staff page.")
        if st.button("Start ➜", type="primary", disabled=room is None):
            try:
                rooms.claim_room(room, account["staff_id"], account["full_name"], station)
            except rooms.RoomUnavailable as e:
                st.error(str(e))
                st.stop()
            st.session_state["staff"] = {"station": station, "room": room}
            st.rerun()
    st.stop()

MY_NAME, MY_STATION, MY_ROOM = staff_label(account), shift["station"], shift["room"]
st.sidebar.markdown(
    f"<span class='cq-provider'>{escape(label(MY_STATION))}</span> "
    f"<span class='cq-provider'>🚪 {escape(MY_ROOM)}</span>",
    unsafe_allow_html=True,
)
if st.sidebar.button("Change station / room", width="stretch"):
    rooms.release_room(account["staff_id"])
    st.session_state.pop("staff", None)
    st.rerun()

c1, c2, c3 = st.columns([1, 1, 2])
if c1.button("🔄 Refresh now", width="stretch"):
    st.rerun()
if c2.button("👥 Load demo patients", width="stretch"):
    with st.spinner("Registering demo patients…"):
        load_demo_patients(sms)
    st.rerun()
auto_refresh = c3.toggle(f"Auto-refresh every {AUTO_REFRESH_SECONDS} s", value=True,
                         help="Keeps your queue up to date. Turn off while filling in a long form.")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def format_target(minutes: int) -> str:
    if minutes == 0:
        return "immediately"
    return f"≤ {minutes // 60} h" if minutes >= 60 else f"≤ {minutes} min"


def hhmm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "—"


def full_name(p: dict) -> str:
    return f"{p['first_name']} {p.get('surname') or ''}".strip()


def wait_bar(p: dict) -> str:
    target = TARGET_MINUTES[effective_urgency(p)]
    pct = 100 if target == 0 else min(100, round(wait_minutes(p) / target * 100))
    if is_overdue(p):
        colour = "linear-gradient(90deg,#fb7185,#e11d48)"
    elif pct >= 50:
        colour = "linear-gradient(90deg,#fcd34d,#f59e0b)"
    else:
        colour = "linear-gradient(90deg,#5eead4,#0d9488)"
    return f"<div class='cq-bar'><div style='width:{max(pct, 4)}%;background:{colour}'></div></div>"


def card_header(p: dict, extra_chips: str = "", position: int | None = None) -> str:
    urgency = effective_urgency(p).value
    style = URGENCY_STYLE[urgency]
    pregnant = " · 🤰 pregnant" if p["pregnant"] == "Yes" else ""
    conditions = ", ".join(json.loads(p["chronic_conditions"])) or "none"
    right = urgency_badge(urgency)
    if position is not None:
        right += " " + chip(f"Position {position}", "slate")
    if is_overdue(p):
        right += " " + chip(f"⏰ OVERDUE +{minutes_over_target(p)} min", extra_class="cq-overdue")
    return (
        f"<div class='cq-head'>"
        f"<div class='cq-qnum' style='background:{style['gradient']};color:{style['fg']}'>"
        f"#{p['queue_number']}</div>"
        f"<div class='cq-who'><div class='cq-name'>{escape(full_name(p))}</div>"
        f"<div class='cq-meta'>{p['age']}y · {p['sex']}{pregnant} · ID {mask_id(p.get('id_number'))} · "
        f"📱 {mask_phone(p['phone'])} · 🩺 {escape(conditions)}</div></div><div>{right}</div></div>"
        f"<div class='cq-complaint'><span>Chief complaint</span><br>{escape(p['chief_complaint'] or '—')}</div>"
        f"<div class='cq-chips'>{extra_chips}</div>"
    )


def wait_line(p: dict) -> str:
    staff_note = " (entered by staff)" if p.get("arrival_entered_by_staff") else ""
    target = format_target(TARGET_MINUTES[effective_urgency(p)])
    return (f"<div class='cq-wait'>🕘 Arrived <b>{hhmm(p['arrival_time'])}</b>{staff_note} · ⏱️ in clinic "
            f"<b>{wait_minutes(p)} min</b> · target {target}{wait_bar(p)}</div>")


def vitals_chips(p: dict) -> str:
    if not p.get("vitals_json"):
        return chip("Vitals not taken yet", "slate")
    v = json.loads(p["vitals_json"])
    text = (f"BP {v['bp_systolic']}/{v['bp_diastolic']} · {v['temperature']}°C"
            + (f" · pulse {v['pulse']}" if v.get("pulse") else "")
            + f" · urine infection: {v['urine_infection']} · pregnancy: {v['urine_pregnancy']}")
    chips = chip("🩺 " + text, "teal")
    for f in v.get("flags", []):
        chips += " " + chip("⚠️ " + f["name"], "pink")
    if v.get("note"):
        chips += " " + chip("📝 Vitals note", "amber")
    return chips + (f" {chip('by ' + p['vitals_by'], 'slate')}" if p.get("vitals_by") else "")


def vitals_note_box(p: dict) -> str:
    """The vitals nurse's note, shown clearly to the Nurse/Doctor who prescribes."""
    if not p.get("vitals_json"):
        return ""
    note = json.loads(p["vitals_json"]).get("note")
    if not note:
        return ""
    return (f"<div class='cq-note'>📝 <b>Note from the vitals nurse"
            f"{' – ' + escape(p['vitals_by']) if p.get('vitals_by') else ''}:</b><br>{escape(note)}</div>")


def details_expander(p: dict) -> None:
    triage = json.loads(p["triage_json"])
    llm = triage.get("llm_output")
    with st.expander("🔎 Patient's own words, translation & questions to ask"):
        st.markdown(f"**Original:** {p['symptoms_text']}")
        for m in triage["rules_result"]["matches"]:
            st.markdown(f"- 🚩 {m['rule_name']} ({m['urgency']}) – “{m['matched_phrase']}” [{m['language']}]")
        if llm:
            st.markdown(f"**Language:** {llm['detected_language']}")
            st.markdown(f"**English:** {llm['english_translation']}")
            st.markdown(f"**Symptoms:** {', '.join(llm['symptoms']) or '—'} · "
                        f"**Duration:** {llm['duration'] or '—'} · "
                        f"**Severity:** {llm['self_reported_severity'] or '—'}/10")
            if llm["questions_for_nurse"]:
                st.markdown("**Questions to ask:**")
                for q in llm["questions_for_nurse"]:
                    st.markdown(f"- {q}")
        else:
            st.error(f"AI failed – manual review needed. {triage.get('llm_error') or ''}")
        if p.get("id_number"):
            earlier = [v for v in db.visits_for_id(p["id_number"]) if v["id"] != p["id"]]
            if earlier:
                st.markdown(f"**Earlier visits:** {len(earlier)} (see Patient Records)")


def empty(message: str) -> None:
    st.markdown(f"<div class='cq-card' style='--cq-accent:#94a3b8;text-align:center'>"
                f"<p style='font-size:1rem'>{message}</p></div>", unsafe_allow_html=True)


def done(message: str, icon: str = "✅") -> None:
    st.toast(message, icon=icon)
    st.rerun()


# ---------------------------------------------------------------------------
# "With you now" – what each station does with the called patient
# ---------------------------------------------------------------------------
def vitals_form(p: dict) -> None:
    female = p["sex"] == "Female"
    with st.form(f"vitals_{p['id']}"):
        st.markdown("**🩺 Vital signs & urine tests**")
        c1, c2, c3, c4 = st.columns(4)
        sys_bp = c1.number_input("BP systolic *", 50, 260, value=None, placeholder="e.g. 120")
        dia_bp = c2.number_input("BP diastolic *", 30, 160, value=None, placeholder="e.g. 80")
        temp = c3.number_input("Temperature °C *", 30.0, 44.0, value=None, step=0.1, format="%.1f",
                               placeholder="e.g. 36.8")
        pulse = c4.number_input("Pulse /min", 20, 250, value=None, placeholder="optional")
        c5, c6 = st.columns(2)
        infection = c5.radio("Urine – infection (dipstick)", ["Not done", "Negative", "Positive"],
                             horizontal=True)
        pregnancy_options = ["Not done", "Negative", "Positive"] if female else ["Not applicable"]
        pregnancy = c6.radio("Urine – pregnancy test", pregnancy_options, horizontal=True)
        st.markdown("**🎨 Triage colour**")
        c7, c8 = st.columns([1, 2])
        colour = c7.selectbox("Colour", ["Use suggestion (automatic)"] + [u.value for u in Urgency],
                              help="The suggestion is the highest of: current colour, vital-sign "
                                   "danger flags and the red-flag rules.")
        reason = c8.text_input("Reason (needed if you choose a lower colour than suggested)")
        note = st.text_area("📝 Note for the Nurse/Doctor (optional)", height=80, max_chars=600,
                            placeholder="e.g. Patient anxious, pain started after lifting boxes, "
                                        "missed BP tablets for 3 days.")
        if st.form_submit_button("Save vitals ➜ send to Nurse/Doctor", type="primary", width="stretch"):
            if sys_bp is None or dia_bp is None or temp is None:
                st.error("Please enter blood pressure and temperature.")
                return
            try:
                vitals = Vitals(bp_systolic=sys_bp, bp_diastolic=dia_bp, temperature=temp, pulse=pulse,
                                urine_infection=infection, urine_pregnancy=pregnancy)
                chosen = None if colour.startswith("Use") else Urgency(colour)
                result = record_vitals(p["id"], vitals, MY_NAME, sms, chosen, reason, note)
            except (ValidationError, ValueError) as e:
                msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
                st.error(msg.removeprefix("Value error, "))
                return
            flags = "; ".join(f.name for f in result["flags"]) or "no danger signs"
            done(f"#{p['queue_number']}: {result['final'].value} ({flags}) – sent to Nurse/Doctor", "🩺")


def route_text(stops: list[str]) -> str:
    return " ➜ ".join([label(s) for s in stops] + ["🏠 Home"])


def clinician_form(p: dict) -> None:
    still_to_visit = json.loads(p["pending_stations"] or "[]")  # e.g. pharmacy, after an HIV referral back
    with st.form(f"clinician_{p['id']}"):
        notes = st.text_area("📝 Assessment & prescription *", height=110, value=p.get("clinician_notes") or "",
                             placeholder="e.g. UTI. Ciprofloxacin 500 mg twice daily for 3 days. Review if worse.")
        st.markdown("**Refer to (the patient goes in this order):**")
        cols = st.columns(len(AFTER_CLINICIAN))
        ticked = {s: cols[i].checkbox(f"{i + 1}. {label(s)}", value=s in still_to_visit, key=f"stop_{s}_{p['id']}")
                  for i, s in enumerate(AFTER_CLINICIAN)}
        st.caption("Nothing ticked = the patient goes home. Each room calls the patient and then sends them "
                   "on to the next one.")
        stops = [s for s in AFTER_CLINICIAN if ticked[s]]
        if st.form_submit_button("Save & send ➜", type="primary", width="stretch"):
            if not notes.strip():
                st.error("Please write the assessment / prescription.")
                return
            nxt = complete_clinician(p["id"], notes, stops, MY_NAME, sms)
            done(f"#{p['queue_number']}: {route_text(stops)}" if nxt
                 else f"#{p['queue_number']} discharged – going home", "👩‍⚕️")


def station_form(p: dict) -> None:
    pending = json.loads(p["pending_stations"] or "[]")
    st.markdown(
        "<div class='cq-why'>📝 <b>Prescription / notes"
        f"{' by ' + escape(p['clinician_name']) if p.get('clinician_name') else ''}:</b><br>"
        f"{escape(p.get('clinician_notes') or '—')}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='cq-chips'>{chip('Route: ' + route_text([MY_STATION] + pending), 'violet')}</div>",
                unsafe_allow_html=True)
    next_label = label(pending[0]) if pending else "🏠 home"
    with st.form(f"station_{p['id']}"):
        note = st.text_input("Note (optional)", placeholder="e.g. Medication dispensed, explained dosage")
        b1, b2 = st.columns(2)
        finished = b1.form_submit_button(f"✔️ Done ➜ {next_label}", type="primary", width="stretch")
        back = b2.form_submit_button("↩️ Send back to Nurse/Doctor", width="stretch")
        if finished:
            nxt = complete_station(p["id"], MY_NAME, sms, note.strip())
            done(f"#{p['queue_number']} sent to {label(nxt)}" if nxt
                 else f"#{p['queue_number']} discharged – going home", "✔️")
        if back:
            if not note.strip():
                st.error("Please write a note saying why (e.g. the test result needs discussion).")
                return
            send_back_to_clinician(p["id"], MY_NAME, sms, note.strip())
            done(f"#{p['queue_number']} sent back to Nurse/Doctor", "↩️")


def hiv_result_box(p: dict) -> str:
    """HIV result for the prescriber. Shown only to roles allowed to see it."""
    if not p.get("hiv_test_json"):
        return ""
    if not can_see_hiv_result(account["role"]):
        return "<div class='cq-why'>🧪 HIV test done – 🔒 result restricted to clinical staff.</div>"
    h = json.loads(p["hiv_test_json"])
    colour = "#e11d48" if h["result"].startswith("Positive") else "#0d9488"
    return (f"<div class='cq-note' style='border-left-color:{colour};background:#fff1f2'>🧪 <b>HIV test "
            f"({escape(h['tested_by'])}, {h['time'][11:16]}): {escape(h['result'])}</b>"
            + (f"<br>{escape(h['note'])}" if h.get("note") else "")
            + ("<br>➜ Start ART and link to care." if h["result"].startswith("Positive") else "")
            + "</div>")


def hiv_form(p: dict) -> None:
    pending = json.loads(p["pending_stations"] or "[]")
    st.markdown(
        "<div class='cq-why'>📝 <b>Referral notes"
        f"{' by ' + escape(p['clinician_name']) if p.get('clinician_name') else ''}:</b><br>"
        f"{escape(p.get('clinician_notes') or '—')}</div>"
        f"<div class='cq-chips'>{chip('Route: ' + route_text([MY_STATION] + pending), 'violet')}</div>",
        unsafe_allow_html=True,
    )
    with st.form(f"hiv_{p['id']}"):
        st.markdown("**🧪 HIV counselling & testing** – 🔒 the result is only visible to clinical staff")
        consented = st.checkbox("Pre-test counselling done and the patient consented to testing")
        result = st.radio("Result *", HIV_RESULTS, index=None)
        note = st.text_input("Post-test counselling note (optional)",
                             placeholder="e.g. Post-test counselling done, partner testing discussed")
        next_room = label(pending[0]) if pending else "🏠 home"
        where = st.radio("After this room", [f"➜ Continue to {next_room}", "↩️ Back to the Nurse/Doctor"],
                         help="A POSITIVE result must go back to the Nurse/Doctor for ART initiation.")
        if st.form_submit_button("Save result ➜", type="primary", width="stretch"):
            try:
                nxt = record_hiv_test(p["id"], result or "", consented, MY_NAME, sms,
                                      back_to_clinician=where.startswith("↩️"), note=note)
            except ValueError as e:
                st.error(str(e))
                return
            done(f"#{p['queue_number']} sent to {label(nxt)}" if nxt
                 else f"#{p['queue_number']} discharged – going home", "🧪")


def emergency_actions(p: dict) -> None:
    contacts = load_contacts()
    e1, e2 = st.columns(2)
    with e1.expander("🚑 Refer to hospital"):
        with st.form(f"refer_{p['id']}"):
            hospital = st.selectbox("Hospital", [h["name"] for h in contacts["hospitals"]])
            reason = st.text_input("Reason for referral *")
            if st.form_submit_button("Request ambulance & alert hospital", type="primary"):
                try:
                    refer_to_hospital(p["id"], hospital, reason, MY_NAME, sms)
                except ValueError as e:
                    st.error(str(e))
                    return
                done("Ambulance coordinator alerted – see the Alerts page", "🚑")
    with e2.expander("🕊️ Record death"):
        with st.form(f"death_{p['id']}"):
            when = st.time_input("Time of death", value=datetime.now().time(), step=60)
            unnatural = st.checkbox("Unnatural or suspicious death (police must be informed)")
            sure = st.checkbox("I confirm this patient has died *")
            if st.form_submit_button("Record death & alert facility manager"):
                if not sure:
                    st.error("Please tick the confirmation box.")
                    return
                record_death(p["id"], when.strftime("%H:%M"), unnatural, MY_NAME, sms)
                done("Facility manager alerted; follow-up tasks created on the Alerts page", "🕊️")


def render_with_me(p: dict) -> None:
    triage_chips = (f"{chip('decided by ' + p['decided_by'], 'blue')} "
                    f"<span class='cq-chip cq-chip-slate'>rules</span> "
                    f"{urgency_badge(p['rules_urgency']) if p['rules_urgency'] else chip('none', 'slate')} "
                    f"<span class='cq-chip cq-chip-slate'>AI</span> {urgency_badge(p['ai_urgency'])}")
    extra = triage_chips if MY_STATION == "VITALS" else vitals_chips(p)
    with st.container(key=f"card_{effective_urgency(p).value}_{p['id']}"):
        st.markdown(card_header(p, extra) + wait_line(p)
                    + (vitals_note_box(p) if MY_STATION != "VITALS" else "")
                    + (hiv_result_box(p) if MY_STATION in ("CLINICIAN", "HIV_TEST") else "")
                    + (f"<div class='cq-why'>💡 {escape(p['explanation'] or '')}</div>"
                       if MY_STATION in ("VITALS", "CLINICIAN") else ""),
                    unsafe_allow_html=True)
        if MY_STATION in ("VITALS", "CLINICIAN"):
            details_expander(p)
        if MY_STATION == "VITALS":
            vitals_form(p)
        elif MY_STATION == "CLINICIAN":
            clinician_form(p)
        elif MY_STATION == "HIV_TEST":
            hiv_form(p)
        else:
            station_form(p)
        if MY_STATION in ("VITALS", "CLINICIAN"):
            emergency_actions(p)
        if st.button("↩️ Not here – back to queue", key=f"nothere_{p['id']}",
                     help="The patient did not come. They keep their place in the queue."):
            not_here(p["id"])
            done(f"#{p['queue_number']} returned to the queue – {MY_ROOM} is free", "↩️")


# ---------------------------------------------------------------------------
# Waiting list for my station
# ---------------------------------------------------------------------------
def render_waiting(p: dict, position: int, is_next: bool, busy: bool) -> None:
    chips = chip("👉 NEXT TO CALL", "pink") if is_next else ""
    if moved_up_for_waiting(p):
        chips += " " + chip("⬆️ Moved up for waiting", "pink")
    if not p["nurse_reviewed"]:
        chips += " " + chip("⏳ Colour not yet confirmed – confirm at vitals", "amber")
    if p.get("vitals_json") and MY_STATION != "VITALS":
        chips += " " + vitals_chips(p)
    with st.container(key=f"card_{effective_urgency(p).value}_{p['id']}"):
        st.markdown(card_header(p, chips, position) + wait_line(p), unsafe_allow_html=True)
        if st.button("📣 Call now", key=f"call_{p['id']}", type="primary" if is_next else "secondary",
                     disabled=busy, help=f"{MY_ROOM} is busy – finish with your current patient first."
                     if busy else f"Call to {MY_ROOM}."):
            try:
                call_patient(p["id"], sms, room=MY_ROOM, staff=MY_NAME)
                done(f"Called #{p['queue_number']} to {MY_ROOM} – SMS sent (simulated)", "📣")
            except CallError as e:
                st.session_state["call_error"] = str(e)
                st.rerun()


# ---------------------------------------------------------------------------
# The live part of the page – re-runs on its own every AUTO_REFRESH_SECONDS
# ---------------------------------------------------------------------------
def live_dashboard() -> None:
    if session_expired():  # signed out for inactivity: reload the whole page to show the sign-in form
        st.rerun(scope="app")
    if not rooms.heartbeat(account["staff_id"]):  # keep my room reserved; lost it? choose again
        st.rerun(scope="app")
    log_new_overdue()
    stats = queue_stats()
    by_room = db.called_by_room()
    with_me = by_room.get(MY_ROOM)
    open_alerts = db.count_open_alerts()

    # --- Clinic overview -------------------------------------------------------
    tiles([tile(STATIONS[s]["label"], stats["per_station"][s], STATIONS[s]["icon"],
                "teal" if s == MY_STATION else "blue") for s in STATIONS]
          + [tile("Open alerts", open_alerts, "🚨", "RED" if open_alerts else "slate"),
             tile("Overdue", stats["overdue"], "⏰", "pink")])
    if open_alerts:
        text = (f"🚨 {open_alerts} open alert(s) – ambulance, hospital or death notifications "
                "need attention. Open Alerts ➜")
        try:
            st.page_link("views/4_Alerts.py", label=text)
        except StreamlitAPIException:  # page opened on its own, outside app.py's page list
            st.error(text)

    # Every room: who is working there and which patient is inside.
    room_chips = []
    for r in rooms.room_status():
        name, claim, patient = r["name"], r["claim"], r["patient"]
        you = " (you)" if name == MY_ROOM else ""
        if patient:
            room_chips.append(chip(f"🚪 {name}{you} · {claim['staff_name'] if claim else '—'}: "
                                   f"#{patient['queue_number']}", "violet"))
        elif claim:
            room_chips.append(chip(f"🚪 {name}{you} · {claim['staff_name']}: free", "teal"))
        else:
            room_chips.append(chip(f"🚪 {name}: empty", "slate"))
    with st.expander(f"🚪 Rooms – {sum(1 for r in rooms.room_status() if r['claim'])} of "
                     f"{len(rooms.load_rooms())} in use"):
        st.markdown(f"<div class='cq-chips'>{' '.join(room_chips)}</div>", unsafe_allow_html=True)

    # --- With you now ----------------------------------------------------------
    if with_me:
        st.markdown(f"<div class='cq-section'>🚪 With you now in {escape(MY_ROOM)}</div>",
                    unsafe_allow_html=True)
        if with_me["station"] != MY_STATION:
            st.warning(f"This patient was called to {MY_ROOM} for {label(with_me['station'])}. "
                       "Sign in to that station to continue, or press 'Not here'.")
            if st.button("↩️ Not here – back to queue", key="nothere_other"):
                not_here(with_me["id"])
                st.rerun()
        else:
            render_with_me(with_me)

    # --- What the colours mean (always visible above the queue) ----------------
    colour_key(full=False)

    # --- My station's queue ---------------------------------------------------
    waiting = get_waiting_queue(station=MY_STATION)
    st.markdown(f"<div class='cq-section'>{label(MY_STATION)} queue ({len(waiting)}) "
                f"<small>{STATIONS[MY_STATION]['help']} · most urgent first</small></div>",
                unsafe_allow_html=True)
    if "call_error" in st.session_state:
        st.error("🚪 " + st.session_state.pop("call_error"))
    if with_me:
        st.caption(f"Finish with #{with_me['queue_number']} before calling the next patient "
                   "(one patient per room, for privacy).")
    if not waiting:
        empty("🎉 Nobody is waiting for your station right now.")
    for i, p in enumerate(waiting):
        render_waiting(p, i + 1, is_next=(i == 0), busy=with_me is not None)

    # --- Finished today --------------------------------------------------------
    today = date.today().isoformat()
    finished = sorted((p for p in db.list_patients(("done",)) if (p["seen_time"] or "").startswith(today)),
                      key=lambda p: p["seen_time"], reverse=True)
    icons = {"DISCHARGED": "🏠 Home", "REFERRED": "🚑 Referred", "DECEASED": "🕊️ Died"}
    with st.expander(f"✔️ Finished today ({len(finished)})"):
        if not finished:
            st.caption("No visits finished yet today.")
        else:
            rows = "".join(
                f"<tr><td><b>#{p['queue_number']}</b></td><td>{escape(full_name(p))}</td>"
                f"<td>{urgency_badge(effective_urgency(p).value)}</td>"
                f"<td>{icons.get(p['outcome'], p['outcome'] or '—')}</td>"
                f"<td>{hhmm(p['arrival_time'])}</td><td>{hhmm(p['seen_time'])}</td></tr>"
                for p in finished
            )
            st.markdown("<table class='cq-table'><tr><th>No.</th><th>Name</th><th>Colour</th>"
                        f"<th>Outcome</th><th>Arrived</th><th>Finished</th></tr>{rows}</table>",
                        unsafe_allow_html=True)


# Only this part re-runs on the timer. While a patient is with you, the refresh pauses so
# nothing interrupts the vitals / prescription form you are filling in.
busy_now = db.patient_in_room(MY_ROOM) is not None
if auto_refresh and busy_now:
    st.caption("⏸️ Auto-refresh is paused while a patient is with you.")
st.fragment(live_dashboard,
            run_every=AUTO_REFRESH_SECONDS if auto_refresh and not busy_now else None)()

with st.sidebar.expander("📱 Simulated SMS outbox", expanded=False):
    if isinstance(sms, MockSMSService):
        messages = sms.recent_messages(15)
        for line in messages or ["No SMS sent yet."]:
            st.caption(line)
    else:
        st.caption(f"SMS provider: {sms.name}")
