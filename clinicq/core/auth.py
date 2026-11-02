"""Staff accounts and authentication (staff ID + personal PIN).

Security measures:
- PINs are never stored. Only a salted PBKDF2-SHA256 hash is kept, and PINs are
  compared in constant time.
- Weak PINs (1234, 0000, 1111, 2468 ...) are refused.
- After MAX_FAILED_ATTEMPTS wrong PINs the account is locked for LOCK_MINUTES.
  Unknown and deactivated staff IDs are throttled and locked in exactly the same way,
  and still run a full PIN check, so neither the messages nor the response time
  reveal which staff IDs exist.
- A PIN set by an administrator (new account or reset) must be changed by the staff
  member at their next sign-in, so the administrator never knows anyone's working PIN.
- Changing your own PIN needs the current PIN, with the same lockout.
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
    "Registration clerk": [],   # registers patients at reception (Patient Intake)
    "Admin": ALL_STATIONS,
}

# Which pages each role may open (pages not listed here are open to every signed-in role).
PAGE_ROLES = {
    "intake": {"Registration clerk", "Nurse", "Doctor", "Facility manager", "Admin"},
    "records": {"Nurse", "Doctor", "Facility manager", "Admin"},
    "export": {"Facility manager", "Admin"},      # bulk CSV export of patient records
    "audit": {"Facility manager", "Admin"},
    "reports": {"Facility manager", "Admin"},
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
    last_login      TEXT,
    must_change_pin INTEGER NOT NULL DEFAULT 0
);

-- Failed sign-ins for staff IDs that do not exist (or are deactivated), so they are
-- throttled exactly like real accounts and cannot be told apart.
CREATE TABLE IF NOT EXISTS login_throttle (
    staff_id        TEXT PRIMARY KEY,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(staff)")}
        if "must_change_pin" not in columns:
            conn.execute("ALTER TABLE staff ADD COLUMN must_change_pin INTEGER NOT NULL DEFAULT 0")


# Used to check a PIN for unknown staff IDs, so a wrong ID takes as long as a wrong PIN.
_DUMMY_HASH = hash_pin(secrets.token_hex(8))


def normalise_staff_id(staff_id: str) -> str:
    staff_id = (staff_id or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,12}", staff_id):
        raise ValueError("Staff ID must be 3–12 letters or digits, e.g. N1001.")
    return staff_id


def add_staff(staff_id: str, full_name: str, role: str, pin: str, created_by: str = "setup",
              must_change_pin: bool = False) -> str:
    """Create an account. must_change_pin=True when an administrator chose the first PIN."""
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
        conn.execute("INSERT INTO staff (staff_id, full_name, role, pin_hash, created, must_change_pin) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (staff_id, full_name, role, hash_pin(pin), db.now_iso(), int(must_change_pin)))
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


def set_pin(staff_id: str, new_pin: str, changed_by: str, must_change: bool = False) -> None:
    """Set a new PIN. must_change=True for an administrator's reset: the staff member
    must choose their own PIN at the next sign-in."""
    check_pin_strength(new_pin)
    _set(staff_id, pin_hash=hash_pin(new_pin), failed_attempts=0, locked_until=None,
         must_change_pin=int(must_change))
    db.add_audit("STAFF_ADMIN", nurse_action=f"PIN for {staff_id} {'reset' if must_change else 'changed'} "
                                             f"by {changed_by}"
                                             + (" (must be changed at next sign-in)" if must_change else ""))


def change_own_pin(staff_id: str, current_pin: str, new_pin: str, now: Optional[datetime] = None) -> None:
    """A signed-in staff member changes their own PIN.

    The current PIN is checked with the same lockout as sign-in, so someone at an
    unattended, signed-in tablet cannot guess it and take over the account.
    Raises AuthError (wrong current PIN / locked) or ValueError (weak or same PIN).
    """
    now = now or datetime.now()
    staff = get_staff(staff_id)
    if not staff or not staff["active"]:
        raise AuthError("This account is not active.")
    _raise_if_locked(staff, now)
    if not verify_pin(current_pin or "", staff["pin_hash"]):
        _count_failure(staff_id, staff["failed_attempts"], now, table="staff", what="current PIN")
    if verify_pin(new_pin or "", staff["pin_hash"]):
        raise ValueError("The new PIN must be different from the current one.")
    set_pin(staff_id, new_pin, staff_label(staff))


def refresh_staff(staff_id: str) -> Optional[dict]:
    """The signed-in person's account as it is NOW (None if deleted or deactivated).

    Called on every page, so deactivating someone or changing their role takes effect
    immediately instead of at their next sign-in.
    """
    staff = get_staff(staff_id)
    if not staff or not staff["active"]:
        return None
    return {k: v for k, v in staff.items() if k != "pin_hash"}


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
def _raise_if_locked(record: dict, now: datetime) -> None:
    if record.get("locked_until") and datetime.fromisoformat(record["locked_until"]) > now:
        minutes = int((datetime.fromisoformat(record["locked_until"]) - now).total_seconds() // 60) + 1
        raise AuthError(f"Too many wrong PINs. This account is locked for {minutes} more minute(s), "
                        "or until an administrator unlocks it.")


def _count_failure(staff_id: str, previous: int, now: datetime, table: str, what: str = "PIN") -> None:
    """Record one wrong PIN (in `staff` or `login_throttle`) and raise the matching AuthError.

    The messages are identical for real and unknown staff IDs.
    """
    failures = previous + 1
    if failures >= MAX_FAILED_ATTEMPTS:
        until = (now + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
        _set_failures(staff_id, table, 0, until)
        db.add_audit("STAFF_LOGIN", nurse_action=f"Account {staff_id} LOCKED after "
                                                 f"{MAX_FAILED_ATTEMPTS} wrong {what}s"
                                                 + (" (unknown or inactive ID)" if table != "staff" else ""))
        raise AuthError(f"Too many wrong PINs. This account is locked for {LOCK_MINUTES} minutes.")
    _set_failures(staff_id, table, failures, None)
    db.add_audit("STAFF_LOGIN", nurse_action=f"Wrong {what} for {staff_id} ({failures}/{MAX_FAILED_ATTEMPTS})"
                                             + (" (unknown or inactive ID)" if table != "staff" else ""))
    left = MAX_FAILED_ATTEMPTS - failures
    if what == "PIN":
        raise AuthError(f"Wrong staff ID or PIN. {left} attempt(s) left before the account is locked.")
    raise AuthError(f"Your current PIN is wrong. {left} attempt(s) left before the account is locked.")


def _set_failures(staff_id: str, table: str, failures: int, locked_until: Optional[str]) -> None:
    if table == "staff":
        _set(staff_id, failed_attempts=failures, locked_until=locked_until)
        return
    with db.get_connection() as conn:
        conn.execute("INSERT INTO login_throttle (staff_id, failed_attempts, locked_until) VALUES (?, ?, ?) "
                     "ON CONFLICT(staff_id) DO UPDATE SET failed_attempts = excluded.failed_attempts, "
                     "locked_until = excluded.locked_until", (staff_id, failures, locked_until))


def _throttle_record(staff_id: str) -> dict:
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM login_throttle WHERE staff_id = ?", (staff_id,)).fetchone()
    return dict(row) if row else {"staff_id": staff_id, "failed_attempts": 0, "locked_until": None}


def authenticate(staff_id: str, pin: str, now: Optional[datetime] = None) -> dict:
    """Check staff ID + PIN. Returns the staff record (without the PIN hash).

    Raises AuthError. Unknown, deactivated and real staff IDs get the same messages,
    the same lockout and (because the PIN is always hashed) the same response time.
    """
    now = now or datetime.now()
    try:
        staff_id = normalise_staff_id(staff_id)
    except ValueError:
        verify_pin(pin or "", _DUMMY_HASH)
        raise AuthError("Wrong staff ID or PIN.") from None
    staff = get_staff(staff_id)
    real = bool(staff and staff["active"])
    record = staff if real else _throttle_record(staff_id)
    _raise_if_locked(record, now)
    correct = verify_pin(pin or "", staff["pin_hash"] if real else _DUMMY_HASH)
    if not (real and correct):
        _count_failure(staff_id, record["failed_attempts"], now, table="staff" if real else "throttle")

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
