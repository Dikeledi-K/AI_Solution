"""Staff accounts.

    Everyone signed in: change their own PIN.
    Admin only:         add staff, reset a PIN, unlock, activate / deactivate.
"""

from html import escape

import streamlit as st

from core import auth
from core import rooms
from core.auth_ui import require_staff
from core.stations import label
from core.ui import hero, page_setup, section, tile, tiles

page_setup("Staff", "👥")
hero("Staff accounts", "Personal staff IDs and PINs – everything is recorded under your name", "👥", "records")
me = require_staff()
ME = auth.staff_label(me)

# ---------------------------------------------------------------------------
# My account
# ---------------------------------------------------------------------------
section("🔑 My PIN")
with st.form("my_pin"):
    c1, c2, c3 = st.columns(3)
    current = c1.text_input("Current PIN *", type="password")
    new = c2.text_input("New PIN (4–8 digits) *", type="password")
    again = c3.text_input("Repeat new PIN *", type="password")
    if st.form_submit_button("Change my PIN", type="primary"):
        if not auth.verify_pin(current, auth.get_staff(me["staff_id"])["pin_hash"]):
            st.error("Your current PIN is wrong.")
        elif new != again:
            st.error("The new PINs do not match.")
        else:
            try:
                auth.set_pin(me["staff_id"], new, ME)
                st.success("PIN changed.")
            except ValueError as e:
                st.error(str(e))

if not auth.can_open(me["role"], "staff_admin"):
    st.caption("Only an Admin can add staff or reset other people's PINs.")
    st.stop()

# ---------------------------------------------------------------------------
# Admin: all staff
# ---------------------------------------------------------------------------
staff = auth.list_staff()
tiles([
    tile("Staff accounts", len(staff), "👥", "blue"),
    tile("Active", sum(s["active"] for s in staff), "✅", "GREEN"),
    tile("Locked", sum(bool(s["locked_until"]) for s in staff), "🔒", "ORANGE"),
])
section("👥 All staff")
st.dataframe(
    [{"Staff ID": s["staff_id"], "Name": s["full_name"], "Role": s["role"],
      "Stations": ", ".join(label(x) for x in auth.allowed_stations(s["role"])) or "— (alerts only)",
      "Active": "Yes" if s["active"] else "No",
      "Locked until": (s["locked_until"] or "")[11:16],
      "Last sign-in": (s["last_login"] or "")[:16].replace("T", " ")} for s in staff],
    width="stretch", hide_index=True,
)

c1, c2 = st.columns(2)
with c1:
    section("➕ Add a staff member")
    if st.button("👥 Add any missing demo staff accounts (classroom only)"):
        n = auth.load_demo_staff()
        st.success(f"{n} demo account(s) added." if n else "All demo accounts already exist.")
        st.rerun()
    with st.form("add_staff", clear_on_submit=True):
        a1, a2 = st.columns(2)
        staff_id = a1.text_input("Staff ID *", placeholder="e.g. N1002")
        name = a2.text_input("Name as shown *", placeholder="e.g. Sr Mokoena")
        role = st.selectbox("Role *", list(auth.ROLES),
                            help="The role decides which stations and pages this person can use.")
        pin = st.text_input("First PIN (4–8 digits) *", type="password",
                            help="Give it to the person privately and ask them to change it on this page.")
        if st.form_submit_button("Add staff member", type="primary"):
            try:
                new_id = auth.add_staff(staff_id, name, role, pin, created_by=ME)
                st.success(f"Added {new_id}.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

with c2:
    section("🛠️ Manage an account")
    if staff:
        options = {s["staff_id"]: f"{s['staff_id']} · {s['full_name']} ({s['role']})" for s in staff}
        chosen = st.selectbox("Staff member", list(options), format_func=options.get)
        person = next(s for s in staff if s["staff_id"] == chosen)
        with st.form("reset_pin", clear_on_submit=True):
            new_pin = st.text_input("New PIN *", type="password")
            if st.form_submit_button("Reset PIN"):
                try:
                    auth.set_pin(chosen, new_pin, ME)
                    st.success(f"PIN reset for {escape(chosen)}. Give it to them privately.")
                except ValueError as e:
                    st.error(str(e))
        b1, b2 = st.columns(2)
        if person["locked_until"] and b1.button("🔓 Unlock"):
            auth.unlock(chosen, ME)
            st.rerun()
        if chosen != me["staff_id"]:
            if person["active"] and b2.button("🚫 Deactivate (left the clinic)"):
                auth.set_active(chosen, False, ME)
                st.rerun()
            if not person["active"] and b2.button("✅ Reactivate"):
                auth.set_active(chosen, True, ME)
                st.rerun()
        else:
            b2.caption("You cannot deactivate your own account.")

# ---------------------------------------------------------------------------
# Admin: rooms
# ---------------------------------------------------------------------------
section("🚪 Rooms")
st.caption("Rooms come from data/rooms.json. A room is freed when its staff member signs out or changes "
           "room. Free a room here if someone went home without signing out.")
status = rooms.room_status()
st.dataframe(
    [{"Room": r["name"], "Type": r["type"], "Stations": ", ".join(label(x) for x in r["stations"]),
      "In use by": r["claim"]["staff_name"] if r["claim"] else "",
      "Patient inside": f"#{r['patient']['queue_number']}" if r["patient"] else ""} for r in status],
    width="stretch", hide_index=True,
)
in_use = [r["name"] for r in status if r["claim"]]
if in_use:
    r1, r2 = st.columns([2, 1])
    to_free = r1.selectbox("Room to free", in_use)
    if r2.button("🔓 Free this room"):
        rooms.admin_release(to_free, ME)
        st.rerun()
