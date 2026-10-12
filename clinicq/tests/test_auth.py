"""Tests for staff accounts and sign-in."""

from datetime import datetime, timedelta

import pytest

from core import auth
from core import database as db


@pytest.fixture
def nurse():
    auth.add_staff("N1001", "Sr Dlamini", "Nurse", "4826")
    return "N1001"


def test_pin_is_never_stored_in_plain_text(nurse):
    with db.get_connection() as conn:
        stored = conn.execute("SELECT pin_hash FROM staff WHERE staff_id = ?", (nurse,)).fetchone()[0]
    assert "4826" not in stored and stored.startswith("pbkdf2_sha256$")
    assert "pin_hash" not in auth.list_staff()[0]


def test_same_pin_gets_a_different_hash_each_time():
    assert auth.hash_pin("4826") != auth.hash_pin("4826")


@pytest.mark.parametrize("weak", ["1234", "0000", "1111", "2468", "4321", "12", "abcd", "123456789"])
def test_weak_pins_are_refused(weak):
    with pytest.raises(ValueError):
        auth.check_pin_strength(weak)


def test_correct_pin_signs_in(nurse):
    staff = auth.authenticate("n1001", "4826")  # staff ID is not case-sensitive
    assert staff["full_name"] == "Sr Dlamini" and staff["role"] == "Nurse"
    assert "pin_hash" not in staff
    assert any("signed in" in (e["nurse_action"] or "") for e in db.get_audit_log("STAFF_LOGIN"))


def test_wrong_pin_and_unknown_id_give_the_same_message(nurse):
    with pytest.raises(auth.AuthError, match="Wrong staff ID or PIN"):
        auth.authenticate("N1001", "9999")
    with pytest.raises(auth.AuthError, match="Wrong staff ID or PIN"):
        auth.authenticate("NOBODY", "9999")


def test_account_locks_after_five_wrong_pins(nurse):
    now = datetime(2026, 1, 1, 8, 0)
    for _ in range(auth.MAX_FAILED_ATTEMPTS - 1):
        with pytest.raises(auth.AuthError, match="attempt"):
            auth.authenticate(nurse, "0001", now=now)
    with pytest.raises(auth.AuthError, match="locked"):
        auth.authenticate(nurse, "0001", now=now)
    # Even the CORRECT PIN is refused while locked
    with pytest.raises(auth.AuthError, match="locked"):
        auth.authenticate(nurse, "4826", now=now + timedelta(minutes=5))
    # ... and works again after the lock expires
    assert auth.authenticate(nurse, "4826", now=now + timedelta(minutes=auth.LOCK_MINUTES + 1))
    assert any("LOCKED" in (e["nurse_action"] or "") for e in db.get_audit_log("STAFF_LOGIN"))


def test_admin_can_unlock(nurse):
    now = datetime.now()
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        with pytest.raises(auth.AuthError):
            auth.authenticate(nurse, "0001", now=now)
    auth.unlock(nurse, "Clinic Admin (ADMIN)")
    assert auth.authenticate(nurse, "4826")


def test_deactivated_staff_cannot_sign_in(nurse):
    auth.set_active(nurse, False, "Clinic Admin (ADMIN)")
    with pytest.raises(auth.AuthError):
        auth.authenticate(nurse, "4826")


def test_pin_change(nurse):
    auth.set_pin(nurse, "7391", "Sr Dlamini (N1001)")
    with pytest.raises(auth.AuthError):
        auth.authenticate(nurse, "4826")
    assert auth.authenticate(nurse, "7391")


def test_duplicate_staff_id_refused(nurse):
    with pytest.raises(ValueError, match="already exists"):
        auth.add_staff("N1001", "Someone Else", "Doctor", "7391")


def test_roles_limit_stations_and_pages():
    assert auth.allowed_stations("Pharmacist") == ["PHARMACY"]
    assert "PHARMACY" not in auth.allowed_stations("Nurse")
    assert auth.allowed_stations("Ambulance coordinator") == []
    assert not auth.can_open("Pharmacist", "records")
    assert auth.can_open("Doctor", "records")
    assert not auth.can_open("Nurse", "audit") and auth.can_open("Facility manager", "audit")
    assert auth.can_open("Admin", "staff_admin") and not auth.can_open("Doctor", "staff_admin")
    assert auth.can_open("Pharmacist", "alerts")  # pages not listed are open to every role


def test_demo_staff_load_once():
    assert auth.load_demo_staff() == 18  # 5 nurses, 2 doctors, 4 pharmacists, 2 counsellors, 3 coordinators ...
    assert auth.load_demo_staff() == 0
    assert auth.authenticate("D2001", "7391")["role"] == "Doctor"


def test_reset_keeps_staff_accounts(nurse):
    db.clear_all()
    assert auth.get_staff(nurse) is not None
