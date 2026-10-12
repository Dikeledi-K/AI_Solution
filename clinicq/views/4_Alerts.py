"""Alerts & referrals: tasks for the ambulance coordinator, facility manager and doctor.

    🚑 Ambulance needed      -> coordinator calls 10177, records the reference & ETA
                                -> ClinicQ then alerts the receiving hospital
    🏥 Notify hospital       -> coordinator confirms the hospital has the handover
    🕊️ Death recorded        -> facility manager attends
    🩺 Certify death / 📞 Inform next of kin / 👮 Report to SAPS -> follow-up tasks
"""

from datetime import datetime
from html import escape

import streamlit as st

from core import database as db
from core.sms_service import get_sms_service
from core.auth import staff_label
from core.auth_ui import require_staff
from core.ui import chip, hero, page_setup, tile, tiles
from core.workflow import ALERT_TYPES, ambulance_called

# Most urgent kinds first.
PRIORITY = ["AMBULANCE", "HOSPITAL", "DEATH_MANAGER", "DEATH_CERTIFY", "NOK_CONTACT", "SAPS"]
COLOURS = {"AMBULANCE": "#e11d48", "HOSPITAL": "#f97316", "DEATH_MANAGER": "#475569",
           "DEATH_CERTIFY": "#475569", "NOK_CONTACT": "#7c3aed", "SAPS": "#1e3a8a"}

page_setup("Alerts", "🚨")
hero("Alerts & referrals", "Ambulance requests, hospital notifications and death follow-ups", "🚨", "alerts")
sms = get_sms_service()

my_name = staff_label(require_staff("alerts"))


def minutes_ago(iso: str) -> int:
    return int((datetime.now() - datetime.fromisoformat(iso)).total_seconds() // 60)


def need_name() -> bool:
    return False  # the signed-in staff member is always known


def render_alert(a: dict) -> None:
    colour = COLOURS.get(a["alert_type"], "#475569")
    patient = f"#{a['queue_number']}" if a["queue_number"] else ""
    age_chip = chip(f"{minutes_ago(a['created'])} min ago",
                    "amber" if a["alert_type"] == "AMBULANCE" else "slate")
    st.markdown(
        f"<div class='cq-card' style='--cq-accent:{colour};margin-bottom:.4rem'>"
        f"<div class='cq-head'><div class='cq-name'>{ALERT_TYPES.get(a['alert_type'], a['alert_type'])} "
        f"{escape(patient)}</div><div>{chip('For: ' + a['recipient'], 'violet')} "
        f"{age_chip}"
        f"</div></div><p style='margin-top:.5rem;font-size:.97rem;color:#0f172a'>{escape(a['message'])}</p></div>",
        unsafe_allow_html=True,
    )
    with st.form(f"alert_{a['id']}"):
        if a["alert_type"] == "AMBULANCE":
            c1, c2 = st.columns(2)
            reference = c1.text_input("Ambulance / EMS reference number *")
            eta = c2.number_input("ETA (minutes)", min_value=1, max_value=240, value=20)
            if st.form_submit_button("🚑 Ambulance called – alert the hospital", type="primary"):
                if need_name():
                    return
                try:
                    ambulance_called(a["id"], reference, int(eta), my_name.strip(), sms)
                except ValueError as e:
                    st.error(str(e))
                    return
                st.toast("Hospital alerted with the referral summary", icon="🏥")
                st.rerun()
        else:
            label = {"HOSPITAL": "✅ Hospital has the handover", "NOK_CONTACT": "✅ Next of kin informed",
                     "SAPS": "✅ SAPS informed", "DEATH_CERTIFY": "✅ Death certified"}.get(a["alert_type"], "✅ Done")
            note = st.text_input("Note", placeholder="e.g. spoke to Sister Mahlaba in casualty")
            if st.form_submit_button(label, type="primary"):
                if need_name():
                    return
                db.close_alert(a["id"], my_name.strip(), note.strip() or None)
                st.toast("Alert closed", icon="✅")
                st.rerun()


def alerts_section() -> None:
    open_alerts = sorted(db.list_alerts("OPEN"),
                         key=lambda a: (PRIORITY.index(a["alert_type"]) if a["alert_type"] in PRIORITY else 99, a["id"]))
    closed = [a for a in db.list_alerts() if a["status"] == "DONE"]
    tiles([
        tile("Open alerts", len(open_alerts), "🚨", "RED" if open_alerts else "slate"),
        tile("Ambulances needed", sum(a["alert_type"] == "AMBULANCE" for a in open_alerts), "🚑", "ORANGE"),
        tile("Hospitals to notify", sum(a["alert_type"] == "HOSPITAL" for a in open_alerts), "🏥", "violet"),
        tile("Death follow-ups", sum(a["alert_type"] in ("DEATH_MANAGER", "DEATH_CERTIFY", "NOK_CONTACT", "SAPS")
                                     for a in open_alerts), "🕊️", "slate"),
        tile("Closed", len(closed), "✅", "GREEN"),
    ])
    if not open_alerts:
        st.markdown("<div class='cq-card' style='--cq-accent:#16a34a;text-align:center'><p style='font-size:1rem'>"
                    "✅ No open alerts. This page checks for new ones every 15 seconds.</p></div>",
                    unsafe_allow_html=True)
    for a in open_alerts:
        render_alert(a)

    with st.expander(f"✅ Closed alerts ({len(closed)})"):
        if not closed:
            st.caption("None yet.")
        for a in closed:
            st.markdown(f"**{ALERT_TYPES.get(a['alert_type'], a['alert_type'])} #{a['queue_number'] or ''}** – "
                        f"closed by {a['handled_by'] or '—'} at {a['handled_time'][11:16]}"
                        f"{' – ' + a['handled_note'] if a['handled_note'] else ''}")


# Check for new alerts automatically – but only while there is nothing to fill in,
# so a form you are typing in is never interrupted.
has_open = db.count_open_alerts() > 0
if has_open:
    st.caption("⏸️ Auto-refresh is paused while alerts are open. Press Refresh to check for new ones.")
    if st.button("🔄 Refresh"):
        st.rerun()
st.fragment(alerts_section, run_every=None if has_open else 15)()
