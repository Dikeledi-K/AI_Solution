"""Audit Log page: every decision, action, alert and sign-in, exportable as CSV.

1. Staff activity: how many patients each staff member helped on a day, and when.
2. The full log: readable columns (time, event, patient, colour, what happened, details).
   The raw rules / AI output used for each triage is in "Technical details".

The facility manager sees everything. The Admin (who manages the system, not patients)
sees the same log with patient names and clinical details hidden.
"""

import csv
import io
from datetime import date, datetime

import pandas as pd
import streamlit as st

from core import database as db
from core.auth import can_see_patient_data, staff_label
from core.auth_ui import require_staff
from core.reports import staff_activity
from core.ui import URGENCY_STYLE, hero, page_setup, section, tile, tiles

page_setup("Audit Log", "📋")
account = require_staff("audit")
hero("Audit Log", "Every decision, action, alert and record view – with timestamps and names", "📋", "audit")
SEES_PATIENTS = can_see_patient_data(account["role"])
HIDDEN = "🔒 hidden"

# Patient details that the Admin must not see (they are kept in the database as they are).
PATIENT_FIELDS = ("patient_name", "input_text", "rules_result", "ai_output", "details")


def redact(row: dict) -> dict:
    if SEES_PATIENTS or row["patient_id"] is None:
        return row
    return {**row, **{k: None for k in PATIENT_FIELDS}, "patient_name": HIDDEN}


all_rows = [redact(r) for r in db.get_audit_log()]

# Tamper check: every entry's hash includes the one before it, so an entry changed or
# removed directly in the database file breaks the chain from that point on.
chain = db.verify_audit_chain()
if chain["ok"]:
    st.success(f"🔒 Audit log intact – all {chain['entries']} entries verified. Entries cannot be changed "
               "or deleted, and any change made directly in the database file would show here.")
else:
    st.error(f"⚠️ Audit log integrity check FAILED at entry {chain['first_bad_id']}. An entry was changed or "
             "removed outside ClinicQ. Report this to the facility manager and information officer.")
if not SEES_PATIENTS:
    st.info("🔒 You are viewing as Admin: patient names and clinical details are hidden. Staff, times and "
            "actions are shown.")

# ---------------------------------------------------------------------------
# 1. Staff activity – who helped how many patients, and when
# ---------------------------------------------------------------------------
section("👩‍⚕️ Staff activity", "how many patients each staff member helped, and when")
day = st.date_input("Day", value=date.today(), max_value=date.today(), format="DD/MM/YYYY", key="activity_day")
activity = staff_activity(day)
if not activity:
    st.info("No staff activity with patients on this day.")
else:
    tiles([
        tile("Staff on duty", len(activity), "👥", "blue"),
        tile("Patient contacts", sum(a["patients"] for a in activity), "🤝", "teal"),
        tile("Busiest", f"{activity[0]['patients']}", "🏅", "violet"),
    ])
    st.caption(f"🏅 Busiest: {activity[0]['staff']} ({activity[0]['role']}). "
               "A patient seen by the nurse and then the doctor counts once for each of them.")
    summary = pd.DataFrame([{
        "Staff member": a["staff"], "Role": a["role"], "Patients helped": a["patients"],
        "First patient": a["first"], "Last patient": a["last"],
        "Avg min per patient": a["avg_minutes"] if a["avg_minutes"] is not None else "—",
        "Signed in": a["signed_in"] or "—", "Signed out": a["signed_out"] or "—",
    } for a in activity])
    st.dataframe(summary, width="stretch", hide_index=True,
                 column_config={"Patients helped": st.column_config.ProgressColumn(
                     "Patients helped", format="%d", min_value=0,
                     max_value=max(a["patients"] for a in activity))})
    for a in activity:
        with st.expander(f"{a['staff']} – {a['role']} – {a['patients']} patient(s), {a['first']}–{a['last']}"):
            st.dataframe(pd.DataFrame([{
                "No.": f"#{v['queue_number']}",
                "Patient": (v["patient_name"] or "—") if SEES_PATIENTS else HIDDEN,
                "From": v["from"], "To": v["to"],
                "Minutes": v["minutes"] if v["minutes"] is not None else "—",
                "What they did": v["did"],
            } for v in a["visits"]]), width="stretch", hide_index=True)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["day", "staff", "role", "queue_number", "patient", "from", "to", "minutes", "what_they_did"])
    for a in activity:
        for v in a["visits"]:
            writer.writerow([day.isoformat(), a["staff"], a["role"], v["queue_number"],
                             (v["patient_name"] or "") if SEES_PATIENTS else "", v["from"], v["to"],
                             v["minutes"] if v["minutes"] is not None else "", v["did"]])

    def log_activity_export() -> None:
        db.add_audit("EXPORT", nurse_action=f"Staff activity {day.isoformat()} exported by {staff_label(account)}",
                     staff=staff_label(account))

    st.download_button("⬇️ Export staff activity (CSV)", data=buffer.getvalue(),
                       file_name=f"clinicq_staff_activity_{day.isoformat()}.csv", mime="text/csv",
                       on_click=log_activity_export)

section("📋 Full audit log")

# Plain-English names for the event codes.
EVENT_NAMES = {
    "TRIAGE": "Registered & triaged", "NURSE_CONFIRM": "Colour confirmed", "NURSE_OVERRIDE": "Colour changed",
    "VITALS": "Vitals taken", "CLINICIAN": "Assessed / prescribed", "STATION": "Sent to next room",
    "STATUS": "Called / status", "SMS": "SMS", "OVERDUE": "Overdue", "HIV_TEST": "HIV test",
    "REFERRED": "Hospital referral", "DECEASED": "Death recorded", "ALERT": "Alert",
    "ROOM": "Room", "STAFF_LOGIN": "Sign-in", "STAFF_ADMIN": "Staff admin", "RECORD_VIEW": "Record viewed",
    "RECORDS_ACCESS": "Records access", "EXPORT": "Export", "RESET": "Data reset",
    "RECORD_LOOKUP": "Folder looked up", "DEMO": "Demo data", "BACKUP": "Backup",
}
STAFF_EVENTS = {"ROOM", "STAFF_LOGIN", "STAFF_ADMIN", "RECORDS_ACCESS", "EXPORT", "RESET", "DEMO", "BACKUP"}


def count(*events: str) -> int:
    return sum(r["event_type"] in events for r in all_rows)


tiles([
    tile("Entries", len(all_rows), "🗂️", "blue"),
    tile("Patients involved", len({r["patient_id"] for r in all_rows if r["patient_id"]}), "👥", "teal"),
    tile("Triage decisions", count("TRIAGE"), "🤖", "violet"),
    tile("Colour changes", count("NURSE_OVERRIDE"), "✏️", "ORANGE"),
    tile("Referrals / deaths", count("REFERRED", "DECEASED"), "🚑", "RED"),
    tile("Sign-ins & record views", count("STAFF_LOGIN", "RECORD_VIEW", "EXPORT"), "🔐", "slate"),
])

# --- Filters ------------------------------------------------------------------
event_types = ["All"] + sorted({r["event_type"] for r in all_rows}, key=lambda e: EVENT_NAMES.get(e, e))
choice = st.segmented_control("Filter by event", event_types, default="All",
                              format_func=lambda e: "All" if e == "All" else EVENT_NAMES.get(e, e)) or "All"
query = st.text_input("🔎 Search patient name, queue number, staff member or action",
                      placeholder="e.g. Mokoena, 12, Sr Dlamini, referral").strip().lower()

rows = all_rows if choice == "All" else [r for r in all_rows if r["event_type"] == choice]
if query:
    def matches(r: dict) -> bool:
        haystack = " ".join(str(r.get(k) or "") for k in
                            ("patient_name", "queue_number", "nurse_action", "details", "event_type")).lower()
        return query in haystack
    rows = [r for r in rows if matches(r)]

st.download_button(
    "⬇️ Export as CSV (all columns)",
    data=db.audit_log_csv(rows),
    file_name=f"clinicq_audit_{datetime.now():%Y%m%d_%H%M}.csv",
    mime="text/csv",
    disabled=not rows,
    type="primary",
)
st.caption(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} shown. "
           "“—” means the value does not apply to that event (e.g. a staff sign-in has no patient).")


# --- Readable table --------------------------------------------------------------
def readable_details(r: dict) -> str:
    """Turn the stored triage summary ('decided_by=…; provider=…') into plain English."""
    text = r["details"] or ""
    if r["event_type"] == "TRIAGE" and "decided_by=" in text:
        parts = dict(p.split("=", 1) for p in text.split("; ") if "=" in p)
        arrival = parts.get("arrival", "")
        staff_note = " (time entered by staff)" if "entered by staff" in arrival else ""
        return (f"Colour decided by: {parts.get('decided_by', '—')} · AI used: {parts.get('provider', '—')}"
                f" ({parts.get('attempts', '?')} attempt(s)) · arrived {arrival[11:16]}{staff_note}"
                + (f" · folder {parts['folder']}" if parts.get("folder") else ""))
    return text or "—"


def readable(r: dict) -> dict:
    staff_event = r["event_type"] in STAFF_EVENTS
    return {
        "Time": r["timestamp"].replace("T", " "),
        "Event": EVENT_NAMES.get(r["event_type"], r["event_type"]),
        "No.": f"#{r['queue_number']}" if r["queue_number"] else "—",
        "Patient": r.get("patient_name") or ("— (staff event)" if staff_event else "—"),
        "Colour": r["final_urgency"] or "—",
        "What happened": r["nurse_action"] or "—",
        "Details": readable_details(r),
    }


EVENT_COLOURS = {
    "Registered & triaged": "#ede9fe", "Colour confirmed": "#ccfbf1", "Colour changed": "#ffedd5",
    "Overdue": "#fce7f3", "Called / status": "#dbeafe", "SMS": "#e2e8f0", "Vitals taken": "#ccfbf1",
    "Assessed / prescribed": "#dbeafe", "Sent to next room": "#e0e7ff", "Hospital referral": "#fee2e2",
    "Death recorded": "#e2e8f0", "Alert": "#ffe4e6", "Record viewed": "#f1f5f9", "Export": "#fef3c7",
    "Records access": "#f1f5f9", "Sign-in": "#f1f5f9", "Room": "#f0fdfa", "HIV test": "#fdf2f8",
    "Staff admin": "#f1f5f9", "Data reset": "#fee2e2", "Folder looked up": "#f1f5f9", "Demo data": "#fef3c7",
    "Backup": "#f0fdfa",
}


def colour_event(value):
    return f"background-color:{EVENT_COLOURS.get(value, '')};font-weight:600"


def colour_urgency(value):
    style = URGENCY_STYLE.get(value)
    return f"background-color:{style['bg']};color:{style['fg']};font-weight:700" if style else "color:#94a3b8"


if rows:
    table = pd.DataFrame([readable(r) for r in rows])
    styled = table.style.map(colour_event, subset=["Event"]).map(colour_urgency, subset=["Colour"])
    st.dataframe(styled, width="stretch", hide_index=True,
                 column_config={"Details": st.column_config.TextColumn(width="large"),
                                "What happened": st.column_config.TextColumn(width="large")})
else:
    st.info("No audit entries match.")

# --- Technical details: what the rules and the AI produced for each triage ------
triage_rows = [r for r in rows if r["event_type"] == "TRIAGE"]
if SEES_PATIENTS:  # the Admin does not see patients' words or the AI output
    with st.expander(f"🔧 Technical details – rules & AI output for each triage ({len(triage_rows)})"):
        st.caption("The patient's own words, the red-flag rules result and the AI's structured output, "
                   "exactly as stored. Useful for checking why a colour was given.")
        if triage_rows:
            st.dataframe(
                pd.DataFrame([{
                    "Time": r["timestamp"].replace("T", " "),
                    "No.": f"#{r['queue_number']}",
                    "Patient": r.get("patient_name") or "—",
                    "Patient's words": r["input_text"] or "—",
                    "Rules result": r["rules_result"] or "—",
                    "AI output": r["ai_output"] or "—",
                } for r in triage_rows]),
                width="stretch", hide_index=True,
            )
        else:
            st.caption("No triage entries in the current selection.")
