"""Sign-in screen shared by every staff page.

    staff = require_staff("records")   # at the top of a page

shows the staff ID + PIN form until the person signs in, checks that their
role may open the page, and signs them out after CLINICQ_SESSION_MINUTES of
inactivity (important on shared clinic tablets).

On every page load the account is read again from the database, so a staff
member who is deactivated (or whose role changes) loses access straight away.
Someone whose PIN was set by an administrator must choose their own PIN first.

The sign-in lives only in this browser session – never in the page URL.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta
from html import escape
from typing import Optional

import streamlit as st

from core import auth, rooms
from core.settings import demo_mode, setup_code

SESSION_MINUTES = int(os.getenv("CLINICQ_SESSION_MINUTES", "30"))

# One browser may not try more than this many wrong sign-ins in BROWSER_WINDOW_MINUTES,
# whichever staff IDs it tries (slows down guessing PINs across many accounts).
BROWSER_MAX_FAILURES = 10
BROWSER_WINDOW_MINUTES = 15

# Session keys that belong to a signed-in person and must be cleared on sign-out.
_SESSION_KEYS = ("auth", "staff", "records_viewed", "intake_prefill")


def sign_out(reason: Optional[str] = None) -> None:
    staff = st.session_state.get("auth", {}).get("staff")
    if staff:
        rooms.release_room(staff["staff_id"])  # free their room for colleagues
        auth.db.add_audit("STAFF_LOGIN", nurse_action=f"{auth.staff_label(staff)} signed out"
                                                      + (f" ({reason})" if reason else ""),
                          staff=auth.staff_label(staff))
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
    """The signed-in staff member as their account is NOW, or None."""
    session = st.session_state.get("auth")
    if not session:
        return None
    if session_expired():
        sign_out(f"no activity for {SESSION_MINUTES} minutes")
        return None
    fresh = auth.refresh_staff(session["staff"]["staff_id"])
    if fresh is None:
        sign_out("this account has been deactivated")
        return None
    session["staff"] = fresh  # picks up a changed role or name immediately
    return fresh


# ---------------------------------------------------------------------------
# First-time setup
# ---------------------------------------------------------------------------
def _setup_panel() -> None:
    """Shown when there are no staff accounts at all yet (real, non-demo systems only)."""
    st.markdown("#### 🛠️ First-time setup")
    code = setup_code()
    if not code:
        st.warning(
            "There are no staff accounts yet, and first-time setup is **locked**. The person installing "
            "ClinicQ must set a one-time setup code in the environment (`CLINICQ_SETUP_CODE` in `.env`, or "
            "in the Streamlit secrets) and reload this page. This stops a stranger who finds a fresh "
            "installation from making themselves administrator.")
        st.caption("For a classroom demo with fictional data, set CLINICQ_DEMO_MODE=1 instead.")
        return
    st.caption("Create the first administrator, who can then add the other staff on the Staff page. "
               "You need the setup code from the person who installed ClinicQ.")
    with st.form("first_admin", border=False):
        entered = st.text_input("Setup code *", type="password")
        c1, c2 = st.columns(2)
        staff_id = c1.text_input("Admin staff ID *", placeholder="e.g. ADMIN")
        name = c2.text_input("Full name *")
        c3, c4 = st.columns(2)
        pin = c3.text_input("PIN (4–8 digits) *", type="password")
        pin2 = c4.text_input("Repeat PIN *", type="password")
        if st.form_submit_button("Create administrator", type="primary"):
            if not hmac.compare_digest(entered.strip().encode(), code.encode()):
                auth.db.add_audit("STAFF_ADMIN", nurse_action="First-time setup refused: wrong setup code")
                st.error("The setup code is wrong.")
            elif pin != pin2:
                st.error("The two PINs do not match.")
            else:
                try:
                    auth.add_staff(staff_id, name, "Admin", pin, created_by="first-time setup")
                    st.success("Administrator created – sign in below.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


# ---------------------------------------------------------------------------
# Sign in
# ---------------------------------------------------------------------------
def _browser_blocked() -> bool:
    cutoff = datetime.now() - timedelta(minutes=BROWSER_WINDOW_MINUTES)
    failures = [t for t in st.session_state.get("signin_failures", []) if t > cutoff]
    st.session_state["signin_failures"] = failures
    return len(failures) >= BROWSER_MAX_FAILURES


def _sign_in_panel() -> None:
    with st.container(key="panel_signin"):
        if auth.staff_count() == 0:
            if demo_mode():
                auth.load_demo_staff()  # demo: accounts from the README, so there is no setup screen
                st.rerun()
            _setup_panel()
            return
        st.markdown("#### 🔐 Staff sign-in")
        st.caption("Sign in with your own staff ID and PIN. Never share your PIN – everything you do "
                   "is recorded under your name.")
        if "signed_out_reason" in st.session_state:
            st.info(f"You were signed out: {st.session_state.pop('signed_out_reason')}.")
        if _browser_blocked():
            st.error(f"Too many wrong sign-ins from this device. Please wait {BROWSER_WINDOW_MINUTES} minutes "
                     "or ask an administrator for help.")
            return
        with st.form("sign_in", border=False):
            c1, c2 = st.columns(2)
            staff_id = c1.text_input("Staff ID *", placeholder="e.g. N1001")
            pin = c2.text_input("PIN *", type="password", max_chars=8)
            if st.form_submit_button("Sign in 🔓", type="primary", width="stretch"):
                try:
                    staff = auth.authenticate(staff_id, pin)
                except auth.AuthError as e:
                    st.session_state.setdefault("signin_failures", []).append(datetime.now())
                    st.error(str(e))
                    return
                st.session_state.pop("signin_failures", None)
                st.session_state["auth"] = {"staff": staff, "last_seen": datetime.now()}
                st.rerun()


def _forced_pin_change(staff: dict) -> None:
    """An administrator chose this person's PIN: they must pick their own before working."""
    with st.container(key="panel_signin"):
        st.markdown(f"#### 🔑 Welcome, {escape(staff['full_name'])} – please choose your own PIN")
        st.caption("Your PIN was set by an administrator. Choose a new one that only you know before you "
                   "continue. Everything you do in ClinicQ is recorded under your name.")
        pin_form(staff, key="forced_pin")


def pin_form(staff: dict, key: str = "my_pin") -> None:
    """Change-my-PIN form (also used on the Staff page)."""
    with st.form(key, clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        current = c1.text_input("Current PIN *", type="password")
        new = c2.text_input("New PIN (4–8 digits) *", type="password")
        again = c3.text_input("Repeat new PIN *", type="password")
        if st.form_submit_button("Change my PIN", type="primary"):
            if new != again:
                st.error("The new PINs do not match.")
                return
            try:
                auth.change_own_pin(staff["staff_id"], current, new)
            except auth.AuthError as e:
                if "locked" in str(e):
                    sign_out("too many wrong PINs – the account is locked")
                    st.rerun()
                st.error(str(e))
                return
            except ValueError as e:
                st.error(str(e))
                return
            st.success("PIN changed.")
            st.rerun()


def require_staff(page: Optional[str] = None) -> dict:
    """Stop the page with a sign-in form unless a permitted staff member is signed in."""
    staff = current_staff()
    if not staff:
        _sign_in_panel()
        st.stop()
    st.session_state["auth"]["last_seen"] = datetime.now()
    _sidebar(staff)
    if staff.get("must_change_pin"):
        _forced_pin_change(staff)
        st.stop()
    if page and not auth.can_open(staff["role"], page):
        st.error(f"🔒 Your role ({staff['role']}) does not have access to this page.")
        st.stop()
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
