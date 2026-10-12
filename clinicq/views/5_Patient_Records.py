"""Patient Records: the clinic's database of every visit, with full personal details.

Only Nurses, Doctors, Facility managers and Admins may open it, after signing in
with their own staff ID and PIN. Every record opened and every export is written
to the audit log under their name (POPIA accountability).

Real use still needs an encrypted database.
"""

import json
from datetime import datetime
from html import escape

import streamlit as st

from core import database as db
from core.auth import can_see_hiv_result, staff_label
from core.auth_ui import require_staff
from core.queue_manager import effective_urgency
from core.stations import label
from core.ui import hero, page_setup, tile, tiles, urgency_badge

page_setup("Patient Records", "🗂️")
hero("Patient Records", "Every visit, with personal details – staff only", "🗂️", "records")

account = require_staff("records")
VIEWER = staff_label(account)
st.markdown(f"🔓 Signed in as **{escape(VIEWER)}** – every record you open is logged under your name.")

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
all_rows = db.search_patients("")
today = datetime.now().date().isoformat()
tiles([
    tile("Visits recorded", len(all_rows), "🗂️", "blue"),
    tile("Today", sum(r["arrival_time"].startswith(today) for r in all_rows), "📅", "teal"),
    tile("Referred", sum(r["outcome"] == "REFERRED" for r in all_rows), "🚑", "ORANGE"),
    tile("Deceased", sum(r["outcome"] == "DECEASED" for r in all_rows), "🕊️", "slate"),
])

query = st.text_input("🔎 Search by name, surname, ID/passport number or queue number",
                      placeholder="e.g. Mokoena, DEMO10001 or 12")
rows = db.search_patients(query)

OUTCOME = {"DISCHARGED": "🏠 Home", "REFERRED": "🚑 Referred", "DECEASED": "🕊️ Died", None: "In clinic"}
table = [{
    "No.": r["queue_number"], "Arrived": r["arrival_time"][:16].replace("T", " "),
    "Name": r["first_name"], "Surname": r.get("surname") or "", "ID type": r.get("id_type") or "",
    "ID number": r.get("id_number") or "", "Age": r["age"], "Sex": r["sex"], "Phone": r.get("phone") or "",
    "Colour": effective_urgency(r).value,
    "Status": OUTCOME.get(r["outcome"]) if r["status"] == "done" else f"At {label(r['station'])}",
} for r in rows]


def log_export() -> None:
    db.add_audit("EXPORT", nurse_action=f"Patient records exported by {VIEWER}",
                 details=f"{len(rows)} record(s); search='{query}'")


c1, c2 = st.columns([3, 1])
c1.caption(f"{len(rows)} visit(s) found.")
c2.download_button("⬇️ Export CSV", data=db.records_csv(rows), on_click=log_export,
                   file_name=f"clinicq_records_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv",
                   disabled=not rows, width="stretch")
st.dataframe(table, width="stretch", hide_index=True)

# ---------------------------------------------------------------------------
# One record
# ---------------------------------------------------------------------------
if rows:
    options = {r["id"]: f"#{r['queue_number']} · {r['first_name']} {r.get('surname') or ''} · "
                        f"{r['arrival_time'][:10]}" for r in rows}
    chosen = st.selectbox("📄 Open a record", list(options), format_func=options.get, index=None,
                          placeholder="Choose a visit to see everything recorded")
    if chosen:
        p = db.get_patient(chosen)
        viewed = st.session_state.setdefault("records_viewed", set())
        if chosen not in viewed:  # log each record once per session
            db.add_audit("RECORD_VIEW", patient_id=p["id"], queue_number=p["queue_number"],
                         nurse_action=f"Record viewed by {VIEWER}")
            viewed.add(chosen)

        def field(name: str, value) -> str:
            return f"<tr><th style='width:35%'>{name}</th><td>{escape(str(value)) if value else '—'}</td></tr>"

        left, right = st.columns(2)
        with left:
            st.markdown(
                "<div class='cq-card' style='--cq-accent:#4338ca'><h4>👤 Personal details</h4>"
                "<table class='cq-table'>"
                + field("Name", f"{p['first_name']} {p.get('surname') or ''}")
                + field(p.get("id_type") or "ID", p.get("id_number"))
                + field("Date of birth", p.get("date_of_birth"))
                + field("Age / sex", f"{p['age']} / {p['sex']}")
                + field("Address", p.get("address"))
                + field("Phone", p.get("phone"))
                + field("Consent given", (p.get("consent_time") or "")[:16].replace("T", " "))
                + "</table><h4>👪 Next of kin</h4><table class='cq-table'>"
                + field("Name", p.get("nok_name"))
                + field("Relationship", p.get("nok_relationship"))
                + field("Phone", p.get("nok_phone"))
                + "</table></div>",
                unsafe_allow_html=True,
            )
        with right:
            vitals = json.loads(p["vitals_json"]) if p.get("vitals_json") else None
            vitals_text = (f"BP {vitals['bp_systolic']}/{vitals['bp_diastolic']}, {vitals['temperature']}°C"
                           + (f", pulse {vitals['pulse']}" if vitals.get("pulse") else "")
                           + f"; urine infection {vitals['urine_infection']}; pregnancy {vitals['urine_pregnancy']}"
                           ) if vitals else None
            details = json.loads(p["outcome_details"]) if p.get("outcome_details") else {}
            # HIV results: need-to-know only (nurses, doctors, HIV counsellors). Never in exports.
            hiv = json.loads(p["hiv_test_json"]) if p.get("hiv_test_json") else None
            if not hiv:
                hiv_text = None
            elif can_see_hiv_result(account["role"]):
                hiv_text = f"{hiv['result']} – by {hiv['tested_by']} at {hiv['time'][:16].replace('T', ' ')}"
            else:
                hiv_text = "🔒 Restricted to clinical staff"
            st.markdown(
                "<div class='cq-card' style='--cq-accent:#0d9488'><h4>🩺 Visit</h4><table class='cq-table'>"
                + field("Queue number", p["queue_number"])
                + field("Arrived", p["arrival_time"].replace("T", " "))
                + f"<tr><th>Colour</th><td>{urgency_badge(effective_urgency(p).value)}</td></tr>"
                + field("Complaint", p["chief_complaint"])
                + field("Patient's words", p["symptoms_text"])
                + field("Chronic conditions", ", ".join(json.loads(p["chronic_conditions"])))
                + field("Pregnant", p["pregnant"])
                + field(f"Vitals (by {p.get('vitals_by') or '—'})", vitals_text)
                + field("Vitals nurse's note", vitals.get("note") if vitals else None)
                + field("HIV test", hiv_text)
                + field(f"Prescription (by {p.get('clinician_name') or '—'})", p.get("clinician_notes"))
                + field("Status", OUTCOME.get(p["outcome"]) if p["status"] == "done" else f"At {label(p['station'])}")
                + field("Outcome details", "; ".join(f"{k}: {v}" for k, v in details.items()))
                + "</table></div>",
                unsafe_allow_html=True,
            )

        with st.expander("🧭 Journey & audit trail for this visit"):
            for e in reversed(db.get_audit_log(patient_id=p["id"])):
                st.markdown(f"- `{e['timestamp'][11:16]}` **{e['event_type']}** – "
                            f"{e['nurse_action'] or ''} {('· ' + e['details']) if e['details'] else ''}")
        if p.get("id_number"):
            earlier = [v for v in db.visits_for_id(p["id_number"]) if v["id"] != p["id"]]
            if earlier:
                st.markdown(f"**Earlier visits ({len(earlier)}):** " + ", ".join(
                    f"#{v['queue_number']} on {v['arrival_time'][:10]} ({v['chief_complaint']})" for v in earlier))
