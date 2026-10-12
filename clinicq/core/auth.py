"""Staff accounts and authentication (staff ID + personal PIN).

Security measures:
- PINs are never stored. Only a salted PBKDF2-SHA256 hash is kept, and PINs are
  compared in constant time.
- Weak PINs (1234, 0000, 1111, 2468 ...) are refused.
- After MAX_FAILED_ATTEMPTS wrong PINs the account is locked for LOCK_MINUTES.
- Every sign-in, failure and lock is written to the audit log.
- Each staff member has a ROLE that decides which stations and pages they may use.

Fingerprint sign-in is a future step: it should use WebAuthn / Windows Hello so
the fingerprint never leaves the staff member's own device (see README).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core import database as db
from core.stations import STATIONS

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
PBKDF2_ITERATIONS = 200_000

ALL_STATIONS = list(STATIONS)

# Which stations each role may sign in to.
ROLES = {
    "Nurse": ["VITALS", "CLINICIAN", "INJECTION", "HIV_TEST"],
    "Doctor": ["CLINICIAN", "VITALS", "INJECTION"],
    "Pharmacist": ["PHARMACY"],
    "HIV counsellor": ["HIV_TEST"],
    "Ambulance coordinator": [],
    "Facility manager": [],
    "Admin": ALL_STATIONS,
}

# Which pages each role may open (pages not listed here are open to every signed-in role).
PAGE_ROLES = {
    "records": {"Nurse", "Doctor", "Facility manager", "Admin"},
    "audit": {"Facility manager", "Admin"},
    "staff_admin": {"Admin"},
}

# Who may SEE an HIV test result (highly sensitive – "need to know" only).
HIV_RESULT_ROLES = {"Nurse", "Doctor", "HIV counsellor"}

DEMO_STAFF_PATH = Path(__file__).resolve().parent.parent / "data" / "demo_staff.json"

STAFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    staff_id        TEXT PRIMARY KEY,
    full_name       TEXT NOT NULL,
    role            TEXT NOT NULL,
    pin_hash        TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created         TEXT NOT NULL,
    last_login      TEXT
);
"""


class AuthError(Exception):
    """Sign-in failed. The message is safe to show to the user."""


# ---------------------------------------------------------------------------
# PIN hashing
# ---------------------------------------------------------------------------
def check_pin_strength(pin: str) -> None:
    """Raise ValueError if the PIN is too weak."""
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        raise ValueError("The PIN must be 4 to 8 digits.")
    if len(set(pin)) == 1:
        raise ValueError("The PIN cannot be the same digit repeated (e.g. 0000).")
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if steps in ({1}, {-1}, {2}, {-2}):
        raise ValueError("The PIN cannot be a simple sequence (e.g. 1234, 2468, 4321).")


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


# ---------------------------------------------------------------------------
# Staff accounts
# ---------------------------------------------------------------------------
def _init() -> None:
    db.init_db()
    with db.get_connection() as conn:
        conn.executescript(STAFF_SCHEMA)


def normalise_staff_id(staff_id: str) -> str:
    staff_id = (staff_id or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,12}", staff_id):
        raise ValueError("Staff ID must be 3–12 letters or digits, e.g. N1001.")
    return staff_id


def add_staff(staff_id: str, full_name: str, role: str, pin: str, created_by: str = "setup") -> str:
    _init()
    staff_id = normalise_staff_id(staff_id)
    full_name = full_name.strip()
    if not full_name:
        raise ValueError("Please enter the staff member's name.")
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    check_pin_strength(pin)
    if get_staff(staff_id):
        raise ValueError(f"Staff ID {staff_id} already exists.")
    with db.get_connection() as conn:
        conn.execute("INSERT INTO staff (staff_id, full_name, role, pin_hash, created) VALUES (?, ?, ?, ?, ?)",
                     (staff_id, full_name, role, hash_pin(pin), db.now_iso()))
    db.add_audit("STAFF_ADMIN", nurse_action=f"Staff {staff_id} ({full_name}, {role}) added by {created_by}")
    return staff_id


def get_staff(staff_id: str) -> Optional[dict]:
    _init()
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM staff WHERE staff_id = ?", (staff_id,)).fetchone()
    return dict(row) if row else None


def list_staff() -> list[dict]:
    _init()
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM staff ORDER BY role, full_name").fetchall()
    return [{k: v for k, v in dict(r).items() if k != "pin_hash"} for r in rows]


def staff_count() -> int:
    _init()
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0]


def _set(staff_id: str, **fields) -> None:
    columns = ", ".join(f"{k} = ?" for k in fields)
    with db.get_connection() as conn:
        conn.execute(f"UPDATE staff SET {columns} WHERE staff_id = ?", (*fields.values(), staff_id))


def set_pin(staff_id: str, new_pin: str, changed_by: str) -> None:
    check_pin_strength(new_pin)
    _set(staff_id, pin_hash=hash_pin(new_pin), failed_attempts=0, locked_until=None)
    db.add_audit("STAFF_ADMIN", nurse_action=f"PIN for {staff_id} changed by {changed_by}")


def set_active(staff_id: str, active: bool, changed_by: str) -> None:
    _set(staff_id, active=int(active), failed_attempts=0, locked_until=None)
    db.add_audit("STAFF_ADMIN", nurse_action=f"Staff {staff_id} {'activated' if active else 'deactivated'} "
                                             f"by {changed_by}")


def unlock(staff_id: str, changed_by: str) -> None:
    _set(staff_id, failed_attempts=0, locked_until=None)
    db.add_audit("STAFF_ADMIN", nurse_action=f"Staff {staff_id} unlocked by {changed_by}")


# ---------------------------------------------------------------------------
# Sign in
# ---------------------------------------------------------------------------
def authenticate(staff_id: str, pin: str, now: Optional[datetime] = None) -> dict:
    """Check staff ID + PIN. Returns the staff record (without the PIN hash).

    Raises AuthError with a message that does not reveal whether the ID exists.
    """
    now = now or datetime.now()
    try:
        staff_id = normalise_staff_id(staff_id)
    except ValueError:
        raise AuthError("Wrong staff ID or PIN.") from None
    staff = get_staff(staff_id)
    if not staff or not staff["active"]:
        db.add_audit("STAFF_LOGIN", nurse_action=f"Failed sign-in for {staff_id} (unknown or inactive)")
        raise AuthError("Wrong staff ID or PIN.")

    if staff["locked_until"] and datetime.fromisoformat(staff["locked_until"]) > now:
        minutes = int((datetime.fromisoformat(staff["locked_until"]) - now).total_seconds() // 60) + 1
        raise AuthError(f"Too many wrong PINs. This account is locked for {minutes} more minute(s), "
                        "or until an administrator unlocks it.")

    if not verify_pin(pin or "", staff["pin_hash"]):
        failures = staff["failed_attempts"] + 1
        if failures >= MAX_FAILED_ATTEMPTS:
            until = (now + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
            _set(staff_id, failed_attempts=0, locked_until=until)
            db.add_audit("STAFF_LOGIN", nurse_action=f"Account {staff_id} LOCKED after "
                                                     f"{MAX_FAILED_ATTEMPTS} wrong PINs")
            raise AuthError(f"Too many wrong PINs. This account is locked for {LOCK_MINUTES} minutes.")
        _set(staff_id, failed_attempts=failures)
        db.add_audit("STAFF_LOGIN", nurse_action=f"Wrong PIN for {staff_id} ({failures}/{MAX_FAILED_ATTEMPTS})")
        left = MAX_FAILED_ATTEMPTS - failures
        raise AuthError(f"Wrong staff ID or PIN. {left} attempt(s) left before the account is locked.")

    _set(staff_id, failed_attempts=0, locked_until=None, last_login=now.isoformat(timespec="seconds"))
    db.add_audit("STAFF_LOGIN", nurse_action=f"{staff['full_name']} ({staff_id}, {staff['role']}) signed in")
    return {k: v for k, v in staff.items() if k != "pin_hash"}


def allowed_stations(role: str) -> list[str]:
    return ROLES.get(role, [])


def can_open(role: str, page: str) -> bool:
    allowed = PAGE_ROLES.get(page)
    return allowed is None or role in allowed


def can_see_hiv_result(role: str) -> bool:
    return role in HIV_RESULT_ROLES


def staff_label(staff: dict) -> str:
    """How a staff member appears in the audit log, e.g. 'Sr Dlamini (N1001)'."""
    return f"{staff['full_name']} ({staff['staff_id']})"


# ---------------------------------------------------------------------------
# Demo accounts
# ---------------------------------------------------------------------------
def load_demo_staff() -> int:
    """Create the demo staff accounts from data/demo_staff.json (skips existing IDs)."""
    with open(DEMO_STAFF_PATH, encoding="utf-8") as f:
        people = json.load(f)["staff"]
    added = 0
    for p in people:
        if not get_staff(p["staff_id"]):
            add_staff(p["staff_id"], p["full_name"], p["role"], p["pin"], created_by="demo setup")
            added += 1
    return added
