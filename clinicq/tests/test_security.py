"""Security tests: sign-in enumeration, PIN rules, live account checks, the append-only
audit log, CSV/formula injection and safe display of patient text."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from core import auth
from core import database as db
from core.settings import demo_mode, setup_code
from core.triage import run_triage
from core.ui import md


@pytest.fixture
def nurse():
    auth.add_staff("N1001", "Sr Dlamini", "Nurse", "4826")
    return "N1001"


# ---------------------------------------------------------------------------
# Sign-in never reveals which staff IDs exist
# ---------------------------------------------------------------------------
def _message(staff_id: str, pin: str, now: datetime) -> str:
    with pytest.raises(auth.AuthError) as e:
        auth.authenticate(staff_id, pin, now=now)
    return str(e.value)


def test_real_and_unknown_ids_get_identical_messages_every_time(nurse):
    now = datetime(2026, 1, 1, 8, 0)
    for _ in range(auth.MAX_FAILED_ATTEMPTS + 1):  # through the lockout and beyond
        assert _message("N1001", "9999", now) == _message("NOBODY1", "9999", now)


def test_deactivated_id_looks_exactly_like_an_unknown_id(nurse):
    auth.set_active(nurse, False, "Admin")
    now = datetime(2026, 1, 1, 8, 0)
    assert _message("N1001", "4826", now) == _message("NOBODY1", "4826", now)


def test_unknown_ids_are_locked_out_too(nurse):
    now = datetime(2026, 1, 1, 8, 0)
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        _message("GUESS99", "1357", now)
    assert "locked" in _message("GUESS99", "1357", now)
    assert any("unknown or inactive" in (e["nurse_action"] or "") for e in db.get_audit_log("STAFF_LOGIN"))


# ---------------------------------------------------------------------------
# PINs chosen by an administrator, and changing your own PIN
# ---------------------------------------------------------------------------
def test_admin_chosen_pin_must_be_changed_at_first_sign_in():
    auth.add_staff("R5001", "Ms Nkosi", "Registration clerk", "3958", created_by="Admin", must_change_pin=True)
    assert auth.authenticate("R5001", "3958")["must_change_pin"] == 1
    auth.change_own_pin("R5001", "3958", "6173")
    assert auth.authenticate("R5001", "6173")["must_change_pin"] == 0


def test_admin_pin_reset_forces_a_change(nurse):
    auth.set_pin(nurse, "7391", "Clinic Admin (ADMIN)", must_change=True)
    assert auth.authenticate(nurse, "7391")["must_change_pin"] == 1
    assert any("must be changed" in (e["nurse_action"] or "") for e in db.get_audit_log("STAFF_ADMIN"))


def test_changing_own_pin_needs_the_current_pin_and_locks_after_too_many_guesses(nurse):
    now = datetime(2026, 1, 1, 8, 0)
    for _ in range(auth.MAX_FAILED_ATTEMPTS - 1):
        with pytest.raises(auth.AuthError, match="current PIN is wrong"):
            auth.change_own_pin(nurse, "0001", "7391", now=now)
    with pytest.raises(auth.AuthError, match="locked"):
        auth.change_own_pin(nurse, "0001", "7391", now=now)
    with pytest.raises(auth.AuthError, match="locked"):  # the account itself is now locked
        auth.authenticate(nurse, "4826", now=now + timedelta(minutes=1))


def test_new_pin_must_differ_from_the_current_one(nurse):
    with pytest.raises(ValueError, match="different"):
        auth.change_own_pin(nurse, "4826", "4826")


# ---------------------------------------------------------------------------
# Live account check on every page
# ---------------------------------------------------------------------------
def test_deactivated_staff_lose_access_immediately(nurse):
    assert auth.refresh_staff(nurse)["role"] == "Nurse"
    auth.set_active(nurse, False, "Admin")
    assert auth.refresh_staff(nurse) is None


# ---------------------------------------------------------------------------
# Roles: registration clerks, export and reports
# ---------------------------------------------------------------------------
def test_registration_clerk_registers_but_cannot_see_clinical_records():
    assert auth.can_open("Registration clerk", "intake")
    assert not auth.can_open("Registration clerk", "records")
    assert not auth.can_open("Pharmacist", "intake")
    assert auth.allowed_stations("Registration clerk") == []


def test_only_managers_and_admins_can_bulk_export_or_see_reports():
    for role in ("Nurse", "Doctor", "Registration clerk", "Pharmacist"):
        assert not auth.can_open(role, "export") and not auth.can_open(role, "reports")
    for role in ("Facility manager", "Admin"):
        assert auth.can_open(role, "export") and auth.can_open(role, "reports")


# ---------------------------------------------------------------------------
# Demo mode and first-time setup are off unless configured
# ---------------------------------------------------------------------------
def test_demo_mode_and_setup_code_are_off_by_default(monkeypatch):
    monkeypatch.delenv("CLINICQ_DEMO_MODE", raising=False)
    monkeypatch.delenv("CLINICQ_SETUP_CODE", raising=False)
    assert not demo_mode() and setup_code() is None
    monkeypatch.setenv("CLINICQ_DEMO_MODE", "1")
    monkeypatch.setenv("CLINICQ_SETUP_CODE", " K7-river-42 ")
    assert demo_mode() and setup_code() == "K7-river-42"


# ---------------------------------------------------------------------------
# The audit log is append-only and tamper-evident
# ---------------------------------------------------------------------------
def test_audit_entries_cannot_be_changed_or_deleted():
    db.add_audit("STAFF_LOGIN", nurse_action="Sr Dlamini signed in")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with db.get_connection() as conn:
            conn.execute("UPDATE audit_log SET nurse_action = 'nothing happened'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM audit_log")
    assert len(db.get_audit_log()) == 1


def test_tampering_directly_in_the_file_is_detected():
    for i in range(3):
        db.add_audit("STAFF_LOGIN", nurse_action=f"event {i}")
    assert db.verify_audit_chain() == {"ok": True, "entries": 3, "first_bad_id": None}
    with db.get_connection() as conn:  # someone with the database file bypasses ClinicQ
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET nurse_action = 'edited' WHERE nurse_action = 'event 1'")
    result = db.verify_audit_chain()
    assert not result["ok"] and result["first_bad_id"] == 2


def test_demo_reset_keeps_the_audit_log(make_intake):
    intake = make_intake("I need my repeat medication")
    db.add_patient(intake, run_triage(intake))
    before = len(db.get_audit_log())
    db.clear_all()
    assert db.list_patients() == [] and len(db.get_audit_log()) == before
    assert db.verify_audit_chain()["ok"]


# ---------------------------------------------------------------------------
# Exports and display never run what a patient typed
# ---------------------------------------------------------------------------
def test_csv_exports_neutralise_spreadsheet_formulas(make_intake):
    intake = make_intake("=HYPERLINK(\"http://evil.example\",\"click\") chest pain", first_name="Thandi")
    pid = db.add_patient(intake, run_triage(intake))
    records = db.records_csv([db.get_patient(pid)])
    assert "'=HYPERLINK" in records
    assert "'=HYPERLINK" in db.audit_log_csv()


def test_patient_text_cannot_become_a_link_or_image_on_screen():
    shown = md("![x](http://evil.example/track.png) [click me](http://evil.example)")
    assert "![" not in shown and "](" not in shown and "http://" not in shown
