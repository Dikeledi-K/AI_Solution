"""Sign-in screen shared by every staff page.

    staff = require_staff("records")   # at the top of a page

shows the staff ID + PIN form until the person signs in, checks that their
role may open the page, and signs them out after CLINICQ_SESSION_MINUTES of
inactivity (important on shared clinic computers).

The sign-in lives only in this browser session – never in the page URL.
"""

from __future__ import annotations

import os
from datetime import datetime
from html import escape
from typing import Optional

import streamlit as st

from core import auth, rooms

SESSION_MINUTES = int(os.getenv("CLINICQ_SESSION_MINUTES", "30"))

# Session keys that belong to a signed-in person and must be cleared on sign-out.
_SESSION_KEYS = ("auth", "staff", "records_viewed")


def sign_out(reason: Optional[str] = None) -> None:
    staff = st.session_state.get("auth", {}).get("staff")
    if staff:
        rooms.release_room(staff["staff_id"])  # free their room for colleagues
        auth.db.add_audit("STAFF_LOGIN", nurse_action=f"{auth.staff_label(staff)} signed out"
                                                      + (f" ({reason})" if reason else ""))
    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)
    if reason:
        st.session_state["signed_out_reason"] = reason


def session_expired() -> bool:
    session = st.session_state.get("auth")
    if not session:
        return False
    idle = (datetime.now() - session["last_seen"]).total_seconds() / 60
    return idle > SESSION_MINUTES


def current_staff() -> Optional[dict]:
    session = st.session_state.get("auth")
    if not session:
        return None
    if session_expired():
        sign_out(f"no activity for {SESSION_MINUTES} minutes")
        return None
    return session["staff"]


def _setup_panel() -> None:
    """Shown when there are no staff accounts at all yet."""
    st.markdown("#### 🛠️ First-time setup")
    st.caption("There are no staff accounts yet. Create the first administrator, who can then add "
               "the other staff on the Staff page.")
    with st.form("first_admin", border=False):
        c1, c2 = st.columns(2)
        staff_id = c1.text_input("Admin staff ID *", placeholder="e.g. ADMIN")
        name = c2.text_input("Full name *")
        c3, c4 = st.columns(2)
        pin = c3.text_input("PIN (4–8 digits) *", type="password")
        pin2 = c4.text_input("Repeat PIN *", type="password")
        if st.form_submit_button("Create administrator", type="primary"):
            if pin != pin2:
                st.error("The two PINs do not match.")
            else:
                try:
                    auth.add_staff(staff_id, name, "Admin", pin, created_by="first-time setup")
                    st.success("Administrator created – sign in below.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    st.divider()
    st.caption("For a classroom demo only:")
    if st.button("👥 Load demo staff accounts (PINs are listed in the README)"):
        n = auth.load_demo_staff()
        st.success(f"{n} demo accounts created.")
        st.rerun()


def _sign_in_panel() -> None:
    with st.container(key="panel_signin"):
        if auth.staff_count() == 0:
            _setup_panel()
            return
        st.markdown("#### 🔐 Staff sign-in")
        st.caption("Sign in with your own staff ID and PIN. Never share your PIN – everything you do "
                   "is recorded under your name.")
        if "signed_out_reason" in st.session_state:
            st.info(f"You were signed out: {st.session_state.pop('signed_out_reason')}.")
        with st.form("sign_in", border=False):
            c1, c2 = st.columns(2)
            staff_id = c1.text_input("Staff ID *", placeholder="e.g. N1001")
            pin = c2.text_input("PIN *", type="password", max_chars=8)
            if st.form_submit_button("Sign in 🔓", type="primary", width="stretch"):
                try:
                    staff = auth.authenticate(staff_id, pin)
                except auth.AuthError as e:
                    st.error(str(e))
                    return
                st.session_state["auth"] = {"staff": staff, "last_seen": datetime.now()}
                st.rerun()


def require_staff(page: Optional[str] = None) -> dict:
    """Stop the page with a sign-in form unless a permitted staff member is signed in."""
    staff = current_staff()
    if not staff:
        _sign_in_panel()
        st.stop()
    if page and not auth.can_open(staff["role"], page):
        st.error(f"🔒 Your role ({staff['role']}) does not have access to this page.")
        _sidebar(staff)
        st.stop()
    st.session_state["auth"]["last_seen"] = datetime.now()
    _sidebar(staff)
    return staff


def _sidebar(staff: dict) -> None:
    st.sidebar.markdown(
        f"<span class='cq-provider'>🔐 {escape(staff['full_name'])}</span> "
        f"<span class='cq-provider'>{escape(staff['role'])} · {escape(staff['staff_id'])}</span>",
        unsafe_allow_html=True,
    )
    if st.sidebar.button("🔒 Sign out", key="sign_out_btn", width="stretch"):
        sign_out()
        st.rerun()
    st.sidebar.caption(f"Signs out after {SESSION_MINUTES} min without activity.")
