"""Audit Log page: every decision, action, alert and sign-in, exportable as CSV.

The main table shows readable columns (time, event, patient, colour, what happened,
details). The raw rules / AI output used for each triage is in "Technical details".
"""

from datetime import datetime

import pandas as pd
import streamlit as st

from core import database as db
from core.auth_ui import require_staff
from core.ui import URGENCY_STYLE, hero, page_setup, tile, tiles

page_setup("Audit Log", "📋")
require_staff("audit")
hero("Audit Log", "Every decision, action, alert and record view – with timestamps and names", "📋", "audit")

all_rows = db.get_audit_log()

# Plain-English names for the event codes.
EVENT_NAMES = {
    "TRIAGE": "Registered & triaged", "NURSE_CONFIRM": "Colour confirmed", "NURSE_OVERRIDE": "Colour changed",
    "VITALS": "Vitals taken", "CLINICIAN": "Assessed / prescribed", "STATION": "Sent to next room",
    "STATUS": "Called / status", "SMS": "SMS", "OVERDUE": "Overdue", "HIV_TEST": "HIV test",
    "REFERRED": "Hospital referral", "DECEASED": "Death recorded", "ALERT": "Alert",
    "ROOM": "Room", "STAFF_LOGIN": "Sign-in", "STAFF_ADMIN": "Staff admin", "RECORD_VIEW": "Record viewed",
    "RECORDS_ACCESS": "Records access", "EXPORT": "Export", "RESET": "Data reset",
}
STAFF_EVENTS = {"ROOM", "STAFF_LOGIN", "STAFF_ADMIN", "RECORDS_ACCESS", "EXPORT", "RESET"}


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
                f" ({parts.get('attempts', '?')} attempt(s)) · arrived {arrival[11:16]}{staff_note}")
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
    "Staff admin": "#f1f5f9", "Data reset": "#fee2e2",
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
