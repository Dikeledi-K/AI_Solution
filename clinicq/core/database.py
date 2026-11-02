"""SQLite storage for patients, alerts and the audit log.

Three tables:
    patients   – one row per patient visit: personal details, triage, vitals,
                 where they are in the clinic (station) and how the visit ended
    alerts     – tasks for staff: ambulance requests, hospital notifications,
                 death notifications
    audit_log  – an append-only record of every decision and action

The database file location comes from CLINICQ_DB_PATH (default: data/clinicq.db).

Patient journey (column `station` + `status`):
    VITALS -> CLINICIAN -> (PHARMACY / INJECTION / HIV_TEST ...) -> done
    status = waiting (in a station's queue) | called (in a room) | done (visit over)
    outcome = DISCHARGED | REFERRED | DECEASED  when status = done
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from dotenv import load_dotenv

from core.models import PatientIntake, TriageResult, Urgency

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "clinicq.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_number        INTEGER NOT NULL,       -- starts again at 1 every day
    visit_date          TEXT,                   -- YYYY-MM-DD the patient registered
    folder_number       TEXT,                   -- the clinic folder, the same for every visit
    registered_by       TEXT,                   -- clerk / staff member who registered the visit
    -- personal details (never sent to the AI; masked on dashboards)
    first_name          TEXT NOT NULL,
    surname             TEXT,
    id_type             TEXT,
    id_number           TEXT,
    date_of_birth       TEXT,
    address             TEXT,
    phone               TEXT,
    nok_name            TEXT,
    nok_relationship    TEXT,
    nok_phone           TEXT,
    consent_time        TEXT,
    -- clinical details
    age                 INTEGER NOT NULL,
    sex                 TEXT NOT NULL,
    pregnant            TEXT NOT NULL,
    chronic_conditions  TEXT NOT NULL,          -- JSON list
    symptoms_text       TEXT NOT NULL,
    arrival_time        TEXT NOT NULL,          -- ISO timestamp
    arrival_entered_by_staff INTEGER NOT NULL DEFAULT 0,
    -- triage
    final_urgency       TEXT NOT NULL,          -- rules + AI result
    rules_urgency       TEXT,                   -- NULL = no red flag
    ai_urgency          TEXT NOT NULL,
    decided_by          TEXT NOT NULL,
    chief_complaint     TEXT,
    explanation         TEXT,
    triage_json         TEXT NOT NULL,          -- full TriageResult for the record
    nurse_reviewed      INTEGER NOT NULL DEFAULT 0,
    nurse_urgency       TEXT,                   -- colour confirmed / changed by staff
    override_reason     TEXT,
    -- journey through the clinic
    station             TEXT,                   -- VITALS / CLINICIAN / PHARMACY / INJECTION / HIV_TEST
    pending_stations    TEXT NOT NULL DEFAULT '[]',  -- JSON list of next stops after this one
    status              TEXT NOT NULL DEFAULT 'waiting',  -- waiting / called / done
    room                TEXT,                   -- room the patient was called to
    called_by           TEXT,                   -- staff member who called them
    called_time         TEXT,
    seen_time           TEXT,                   -- when the visit ended
    outcome             TEXT,                   -- DISCHARGED / REFERRED / DECEASED
    outcome_details     TEXT,                   -- JSON (hospital, reason, time of death ...)
    vitals_json         TEXT,
    vitals_by           TEXT,
    clinician_notes     TEXT,                   -- assessment & prescription
    clinician_name      TEXT,
    hiv_test_json       TEXT,                   -- HIV test (RESTRICTED: clinical roles only, never exported)
    near_front_sms_sent INTEGER NOT NULL DEFAULT 0,
    overdue_logged      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created         TEXT NOT NULL,
    alert_type      TEXT NOT NULL,   -- AMBULANCE / HOSPITAL / DEATH_MANAGER / DEATH_CERTIFY / NOK_CONTACT / SAPS
    patient_id      INTEGER,
    queue_number    INTEGER,
    recipient       TEXT NOT NULL,   -- who must act, e.g. "Ambulance coordinator"
    message         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / DONE
    handled_by      TEXT,
    handled_time    TEXT,
    handled_note    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    patient_id      INTEGER,
    queue_number    INTEGER,
    patient_name    TEXT,            -- filled automatically from the patient record
    input_text      TEXT,
    rules_result    TEXT,            -- JSON
    ai_output       TEXT,            -- JSON (or error message)
    final_urgency   TEXT,
    nurse_action    TEXT,
    details         TEXT,
    prev_hash       TEXT,            -- row_hash of the entry before this one
    row_hash        TEXT             -- SHA-256 of this entry + prev_hash (tamper evidence)
);

CREATE TABLE IF NOT EXISTS counters (
    name            TEXT PRIMARY KEY,   -- e.g. 'folder'
    value           INTEGER NOT NULL
);
"""

# The audit log is append-only: the database itself refuses to change or delete an entry.
# (Created after migrations, which are the only code that ever fills in old rows.)
AUDIT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'The audit log is append-only: entries cannot be changed.'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'The audit log is append-only: entries cannot be deleted.'); END;
"""


def get_db_path() -> Path:
    return Path(os.getenv("CLINICQ_DB_PATH", str(DEFAULT_DB_PATH)))


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, always close."""
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Several tablets write at once: wait up to 10 s for a lock instead of failing.
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first version. init_db() adds them to older database files.
MIGRATIONS = {
    "arrival_entered_by_staff": "INTEGER NOT NULL DEFAULT 0",
    "overdue_logged": "INTEGER NOT NULL DEFAULT 0",
    "room": "TEXT",
    "surname": "TEXT", "id_type": "TEXT", "id_number": "TEXT", "date_of_birth": "TEXT",
    "address": "TEXT", "nok_name": "TEXT", "nok_relationship": "TEXT", "nok_phone": "TEXT",
    "consent_time": "TEXT", "station": "TEXT", "pending_stations": "TEXT NOT NULL DEFAULT '[]'",
    "called_by": "TEXT", "outcome": "TEXT", "outcome_details": "TEXT", "vitals_json": "TEXT",
    "vitals_by": "TEXT", "clinician_notes": "TEXT", "clinician_name": "TEXT",
    "hiv_test_json": "TEXT",
    "visit_date": "TEXT", "folder_number": "TEXT", "registered_by": "TEXT",
}

# Audit hash chain: each entry's hash covers its own content AND the previous entry's
# hash, so changing or removing any old entry breaks every hash after it.
GENESIS_HASH = "0" * 64
HASHED_FIELDS = ["timestamp", "event_type", "patient_id", "queue_number", "patient_name", "input_text",
                 "rules_result", "ai_output", "final_urgency", "nurse_action", "details"]


def audit_hash(entry: dict, prev_hash: str) -> str:
    content = json.dumps({k: entry.get(k) for k in HASHED_FIELDS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((prev_hash + content).encode("utf-8")).hexdigest()


# Patients called before rooms existed have room = NULL; treat them as "Room 1".
DEFAULT_ROOM = "Room 1"
FIRST_STATION = "VITALS"


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(patients)")}
        for column, definition in MIGRATIONS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE patients ADD COLUMN {column} {definition}")
        # Early versions stored rooms as bare numbers ("2"); rooms are now named "Room 2".
        conn.execute("UPDATE patients SET room = 'Room ' || room "
                     "WHERE room IS NOT NULL AND room NOT GLOB '*[^0-9]*'")
        _migrate_audit_log(conn)
        conn.executescript(AUDIT_TRIGGERS)
        if "status" in existing:
            # Before stations existed, a finished patient had status 'seen'.
            conn.execute("UPDATE patients SET status = 'done', outcome = COALESCE(outcome, 'DISCHARGED') "
                         "WHERE status = 'seen'")
            conn.execute("UPDATE patients SET station = ? WHERE station IS NULL AND status != 'done'",
                         (FIRST_STATION,))


# What happened, for events that were logged without a description.
DEFAULT_ACTIONS = {
    "TRIAGE": "Registered and triaged",
    "SMS": "SMS sent (simulated)",
    "OVERDUE": "Waited longer than the target time",
}


def _migrate_audit_log(conn) -> None:
    """Bring an older audit log up to date. Runs ONCE per database, before the
    append-only triggers exist, because it is the only code allowed to fill in old rows.

    - adds patient_name and fills in what is reliably known (the colour is NOT
      back-filled: only the patient's colour now is known, not what it was then);
    - adds the tamper-evidence hash chain and hashes the existing entries.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
    if "patient_name" not in columns:
        conn.execute("ALTER TABLE audit_log ADD COLUMN patient_name TEXT")
        conn.execute("""UPDATE audit_log SET patient_name = (
                            SELECT TRIM(p.first_name || ' ' || COALESCE(p.surname, ''))
                            FROM patients p WHERE p.id = audit_log.patient_id)
                        WHERE patient_name IS NULL AND patient_id IS NOT NULL""")
        for event, action in DEFAULT_ACTIONS.items():
            conn.execute("UPDATE audit_log SET nurse_action = ? WHERE event_type = ? AND nurse_action IS NULL",
                         (action, event))
    if "row_hash" not in columns:
        # An older database may already have the triggers from a previous run of this
        # version; drop them just for this one-time back-fill.
        conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
        conn.execute("ALTER TABLE audit_log ADD COLUMN prev_hash TEXT")
        conn.execute("ALTER TABLE audit_log ADD COLUMN row_hash TEXT")
        prev = GENESIS_HASH
        for row in conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall():
            entry = dict(row)
            row_hash = audit_hash(entry, prev)
            conn.execute("UPDATE audit_log SET prev_hash = ?, row_hash = ? WHERE id = ?",
                         (prev, row_hash, entry["id"]))
            prev = row_hash


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
class AlreadyInClinic(ValueError):
    """The person already has an unfinished visit today (stops double registration)."""


def _next_folder_number(conn) -> str:
    """A new clinic folder number, e.g. F000042. Numbers are never reused."""
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('folder', 0)")
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'folder'")
    value = conn.execute("SELECT value FROM counters WHERE name = 'folder'").fetchone()[0]
    return f"F{value:06d}"


def normalise_folder_number(folder: Optional[str]) -> Optional[str]:
    folder = (folder or "").strip().upper()
    return folder or None


def add_patient(
    intake: PatientIntake,
    triage: TriageResult,
    arrival_time: Optional[str] = None,
    arrival_entered_by_staff: bool = False,
    folder_number: Optional[str] = None,
    registered_by: Optional[str] = None,
) -> int:
    """Save a newly registered, triaged patient (they start in the VITALS queue).

    folder_number: the returning patient's existing folder. If it is None but the
    ID/passport number already has a folder, that folder is reused, so one person
    never gets two folders. Otherwise a new folder number is created.
    Raises AlreadyInClinic if that folder already has an unfinished visit.

    Queue numbers start again at 1 every day. Writes a TRIAGE audit entry.
    Returns the patient id.
    """
    init_db()
    arrival_time = arrival_time or now_iso()
    folder_number = normalise_folder_number(folder_number)
    if folder_number is None and intake.id_number:
        folder_number = folder_for_id(intake.id_number)
    if folder_number:
        active = active_visit(folder_number)
        if active:
            raise AlreadyInClinic(
                f"Folder {folder_number} ({active['first_name']} {active.get('surname') or ''}) is already "
                f"in the clinic today as queue number #{active['queue_number']}.")
    llm = triage.llm_output
    values = {
        "first_name": intake.first_name, "surname": intake.surname,
        "id_type": intake.id_type, "id_number": intake.id_number,
        "date_of_birth": intake.date_of_birth, "address": intake.address, "phone": intake.phone,
        "nok_name": intake.nok_name, "nok_relationship": intake.nok_relationship,
        "nok_phone": intake.nok_phone, "consent_time": now_iso(),
        "age": intake.age, "sex": intake.sex, "pregnant": intake.pregnant,
        "chronic_conditions": json.dumps(intake.chronic_conditions),
        "symptoms_text": intake.symptoms_text, "arrival_time": arrival_time,
        "arrival_entered_by_staff": int(arrival_entered_by_staff),
        "final_urgency": triage.final_urgency.value,
        "rules_urgency": triage.rules_result.urgency.value if triage.rules_result.urgency else None,
        "ai_urgency": triage.ai_urgency.value, "decided_by": triage.decided_by,
        "chief_complaint": llm.chief_complaint if llm else "Needs manual review",
        "explanation": triage.explanation(), "triage_json": triage.model_dump_json(),
        "station": FIRST_STATION, "status": "waiting",
        "visit_date": datetime.now().date().isoformat(), "registered_by": registered_by,
    }
    with get_connection() as conn:
        # Lock the database for this whole step, so two clerks registering at the same
        # moment can never get the same queue or folder number.
        conn.execute("BEGIN IMMEDIATE")
        values["queue_number"] = conn.execute(
            "SELECT COALESCE(MAX(queue_number), 0) + 1 FROM patients WHERE visit_date = ?",
            (values["visit_date"],)).fetchone()[0]
        values["folder_number"] = folder_number or _next_folder_number(conn)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = conn.execute(f"INSERT INTO patients ({columns}) VALUES ({placeholders})",
                              tuple(values.values()))
        patient_id = cursor.lastrowid

    add_audit(
        "TRIAGE",
        patient_id=patient_id,
        queue_number=values["queue_number"],
        input_text=intake.symptoms_text,
        rules_result=triage.rules_result.model_dump_json(),
        ai_output=llm.model_dump_json() if llm else f"AI FAILED – fallback used. {triage.llm_error}",
        final_urgency=triage.final_urgency.value,
        nurse_action=f"Registered by {registered_by}" if registered_by else None,
        details=f"decided_by={triage.decided_by}; provider={triage.llm_provider}; "
                f"attempts={triage.llm_attempts}; arrival={arrival_time}"
                f"{' (entered by staff)' if arrival_entered_by_staff else ''}; "
                f"folder={values['folder_number']}",
    )
    return patient_id


# ---------------------------------------------------------------------------
# Returning patients (folder lookup at registration)
# ---------------------------------------------------------------------------
def folder_for_id(id_number: str) -> Optional[str]:
    """The folder already used by this ID/passport/permit number, if any."""
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT folder_number FROM patients WHERE id_number = ? AND folder_number IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1", (id_number.replace(" ", "").upper(),)).fetchone()
    return row[0] if row else None


def active_visit(folder_number: str) -> Optional[dict]:
    """The folder's unfinished visit (waiting or in a room), if any."""
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM patients WHERE folder_number = ? AND status != 'done' "
                           "ORDER BY id DESC LIMIT 1", (folder_number,)).fetchone()
    return dict(row) if row else None


def find_returning_patients(query: str, limit: int = 20) -> list[dict]:
    """People who visited before, matched on folder number, ID number or name.

    Returns the LATEST visit for each folder (one row per person), newest first.
    """
    init_db()
    query = (query or "").strip()
    if len(query) < 2:
        return []
    upper = query.replace(" ", "").upper()
    like = f"%{query}%"
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM patients p
               WHERE p.folder_number IS NOT NULL
                 AND p.id = (SELECT MAX(id) FROM patients q WHERE q.folder_number = p.folder_number)
                 AND (p.folder_number = ? OR p.id_number = ? OR p.first_name LIKE ? OR p.surname LIKE ?
                      OR (p.first_name || ' ' || COALESCE(p.surname, '')) LIKE ?)
               ORDER BY p.arrival_time DESC LIMIT ?""",
            (upper, upper, like, like, like, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def visits_for_folder(folder_number: str) -> list[dict]:
    """Every visit in this folder, newest first."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM patients WHERE folder_number = ? ORDER BY arrival_time DESC, id DESC",
                            (folder_number,)).fetchall()
    return [dict(r) for r in rows]


def get_patient(patient_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    return dict(row) if row else None


def list_patients(statuses: tuple[str, ...] = ("waiting", "called", "done"),
                  station: Optional[str] = None) -> list[dict]:
    init_db()
    placeholders = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM patients WHERE status IN ({placeholders})"
    params: list = list(statuses)
    if station:
        sql += " AND station = ?"
        params.append(station)
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _update(patient_id: int, **fields) -> None:
    columns = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(f"UPDATE patients SET {columns} WHERE id = ?", (*fields.values(), patient_id))


# ---------------------------------------------------------------------------
# Colour decisions
# ---------------------------------------------------------------------------
def set_nurse_urgency(patient_id: int, urgency: Urgency, reason: Optional[str], override: bool,
                      staff: Optional[str] = None) -> None:
    """Record a CONFIRM (same colour) or OVERRIDE (new colour + reason)."""
    patient = get_patient(patient_id)
    # A new colour means a new target time, so an OVERDUE event may need logging again.
    _update(patient_id, nurse_reviewed=1, nurse_urgency=urgency.value, override_reason=reason,
            overdue_logged=0)
    by = f" by {staff}" if staff else ""
    add_audit(
        "NURSE_OVERRIDE" if override else "NURSE_CONFIRM",
        patient_id=patient_id,
        queue_number=patient["queue_number"],
        final_urgency=urgency.value,
        nurse_action=(f"Override {patient['final_urgency']} -> {urgency.value}{by}" if override
                      else f"Confirmed {urgency.value}{by}"),
        details=reason,
    )


# ---------------------------------------------------------------------------
# Rooms and calling
# ---------------------------------------------------------------------------
def call_to_room(patient_id: int, room: str, staff: Optional[str] = None) -> bool:
    """Call a waiting patient into a room – only if that room is free.

    The check and the update happen in ONE SQL statement, so even a double-click
    or two open browser tabs can never put two patients in the same room.
    Returns False if the room is busy (or the patient is no longer waiting).
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE patients SET status = 'called', called_time = ?, room = ?, called_by = ?
               WHERE id = ? AND status = 'waiting'
                 AND NOT EXISTS (SELECT 1 FROM patients
                                 WHERE status = 'called' AND COALESCE(room, ?) = ?)""",
            (now_iso(), room, staff, patient_id, DEFAULT_ROOM, room),
        )
        called = cursor.rowcount == 1
    if called:
        patient = get_patient(patient_id)
        by = f" by {staff}" if staff else ""
        add_audit("STATUS", patient_id=patient_id, queue_number=patient["queue_number"],
                  nurse_action=f"Called to {room} ({patient['station']}){by}")
    return called


def patient_in_room(room: str) -> Optional[dict]:
    """The patient currently called to this room, or None if the room is free."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE status = 'called' AND COALESCE(room, ?) = ?",
            (DEFAULT_ROOM, room),
        ).fetchone()
    return dict(row) if row else None


def called_by_room() -> dict[str, dict]:
    """{room: patient} for every room that currently has a called patient."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM patients WHERE status = 'called'").fetchall()
    return {(row["room"] or DEFAULT_ROOM): dict(row) for row in rows}


def rooms_used_today() -> list[str]:
    """Rooms that have called a patient today (so they show up as quick choices)."""
    init_db()
    today = datetime.now().date().isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(room, ?) FROM patients "
            "WHERE status = 'called' OR substr(called_time, 1, 10) = ?",
            (DEFAULT_ROOM, today),
        ).fetchall()
    return [row[0] for row in rows]


def return_to_queue(patient_id: int) -> None:
    """Patient did not come when called: put them back in the same station's queue.

    Their original arrival time is kept, so they do not lose their place.
    """
    patient = get_patient(patient_id)
    with get_connection() as conn:
        conn.execute(
            "UPDATE patients SET status = 'waiting', called_time = NULL, room = NULL, called_by = NULL "
            "WHERE id = ? AND status = 'called'",
            (patient_id,),
        )
    add_audit("STATUS", patient_id=patient_id, queue_number=patient["queue_number"],
              nurse_action="Not here when called – returned to waiting queue")


# ---------------------------------------------------------------------------
# Moving through the clinic
# ---------------------------------------------------------------------------
def move_to_station(patient_id: int, station: str, pending: list[str], staff: Optional[str] = None,
                    note: str = "") -> None:
    """Send a patient to the queue of another station (frees their current room)."""
    patient = get_patient(patient_id)
    _update(patient_id, station=station, pending_stations=json.dumps(pending), status="waiting",
            room=None, called_time=None, called_by=None, near_front_sms_sent=0)
    by = f" by {staff}" if staff else ""
    add_audit("STATION", patient_id=patient_id, queue_number=patient["queue_number"],
              nurse_action=f"{patient['station']} -> {station}{by}",
              details=(note + (f" Next stops: {', '.join(pending)}" if pending else "")).strip() or None)


def finish_visit(patient_id: int, outcome: str, details: Optional[dict] = None,
                 staff: Optional[str] = None) -> None:
    """End the visit: DISCHARGED (went home), REFERRED (to hospital) or DECEASED."""
    patient = get_patient(patient_id)
    _update(patient_id, status="done", station=None, room=None, pending_stations="[]",
            seen_time=now_iso(), outcome=outcome,
            outcome_details=json.dumps(details) if details else None)
    by = f" by {staff}" if staff else ""
    add_audit(outcome if outcome != "DISCHARGED" else "STATUS", patient_id=patient_id,
              queue_number=patient["queue_number"], nurse_action=f"Visit ended: {outcome}{by}",
              details=json.dumps(details) if details else None)


def save_vitals(patient_id: int, vitals_json: str, staff: Optional[str], pregnant: Optional[str],
                summary: str) -> None:
    fields = {"vitals_json": vitals_json, "vitals_by": staff}
    if pregnant:
        fields["pregnant"] = pregnant
    _update(patient_id, **fields)
    patient = get_patient(patient_id)
    add_audit("VITALS", patient_id=patient_id, queue_number=patient["queue_number"],
              nurse_action=f"Vitals taken by {staff or 'unknown'}", details=summary)


def save_clinician_notes(patient_id: int, notes: str, staff: Optional[str]) -> None:
    _update(patient_id, clinician_notes=notes, clinician_name=staff)
    patient = get_patient(patient_id)
    add_audit("CLINICIAN", patient_id=patient_id, queue_number=patient["queue_number"],
              nurse_action=f"Assessment/prescription by {staff or 'unknown'}", details=notes)


def save_hiv_test(patient_id: int, record_json: str, staff: Optional[str]) -> None:
    """Store an HIV test. The audit log records THAT a result was entered, never the result."""
    _update(patient_id, hiv_test_json=record_json)
    patient = get_patient(patient_id)
    add_audit("HIV_TEST", patient_id=patient_id, queue_number=patient["queue_number"],
              nurse_action=f"HIV test recorded by {staff or 'unknown'}",
              details="Result recorded – restricted to clinical staff")


def mark_near_front_sms_sent(patient_id: int) -> None:
    _update(patient_id, near_front_sms_sent=1)


def mark_overdue_logged(patient_id: int) -> None:
    _update(patient_id, overdue_logged=1)


# ---------------------------------------------------------------------------
# Patient records (full details – only shown on the PIN-protected page)
# ---------------------------------------------------------------------------
RECORD_COLUMNS = [
    "folder_number", "queue_number", "arrival_time", "registered_by", "first_name", "surname", "id_type", "id_number",
    "date_of_birth", "age", "sex", "address", "phone", "nok_name", "nok_relationship", "nok_phone",
    "pregnant", "chronic_conditions", "symptoms_text", "chief_complaint", "final_urgency",
    "nurse_urgency", "vitals_json", "vitals_by", "clinician_notes", "clinician_name", "status",
    "station", "outcome", "outcome_details", "seen_time",
]


def search_patients(query: str = "") -> list[dict]:
    """All visits, newest first, filtered by name, surname, ID number, folder or queue number."""
    init_db()
    q = f"%{query.strip()}%"
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM patients
               WHERE ? = '%%' OR first_name LIKE ? OR surname LIKE ? OR id_number LIKE ?
                  OR CAST(queue_number AS TEXT) = ? OR (first_name || ' ' || COALESCE(surname, '')) LIKE ?
                  OR folder_number = ?
               ORDER BY arrival_time DESC""",
            (q, q, q, q, query.strip(), q, query.strip().upper()),
        ).fetchall()
    return [dict(r) for r in rows]


def visits_for_id(id_number: str) -> list[dict]:
    """Earlier visits by the same person (matched on ID/passport number)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM patients WHERE id_number = ? ORDER BY arrival_time DESC",
                            (id_number,)).fetchall()
    return [dict(r) for r in rows]


def _csv_safe(value):
    """Stop CSV/formula injection: text a patient typed (e.g. "=HYPERLINK(...)") must never
    run as a formula when a manager opens the export in Excel."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _write_csv(rows: list[dict], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({k: _csv_safe(v) for k, v in row.items()} for row in rows)
    return buffer.getvalue()


def records_csv(rows: list[dict]) -> str:
    return _write_csv(rows, RECORD_COLUMNS)


# ---------------------------------------------------------------------------
# Alerts (tasks for staff)
# ---------------------------------------------------------------------------
def add_alert(alert_type: str, recipient: str, message: str, patient: Optional[dict] = None) -> int:
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO alerts (created, alert_type, patient_id, queue_number, recipient, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), alert_type, patient["id"] if patient else None,
             patient["queue_number"] if patient else None, recipient, message),
        )
        alert_id = cursor.lastrowid
    add_audit("ALERT", patient_id=patient["id"] if patient else None,
              queue_number=patient["queue_number"] if patient else None,
              nurse_action=f"{alert_type} alert for {recipient}", details=message)
    return alert_id


def get_alert(alert_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return dict(row) if row else None


def list_alerts(status: Optional[str] = None) -> list[dict]:
    init_db()
    with get_connection() as conn:
        if status:
            rows = conn.execute("SELECT * FROM alerts WHERE status = ? ORDER BY id", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM alerts ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def close_alert(alert_id: int, staff: Optional[str], note: Optional[str]) -> None:
    alert = get_alert(alert_id)
    with get_connection() as conn:
        conn.execute("UPDATE alerts SET status = 'DONE', handled_by = ?, handled_time = ?, "
                     "handled_note = ? WHERE id = ?", (staff, now_iso(), note, alert_id))
    add_audit("ALERT", patient_id=alert["patient_id"], queue_number=alert["queue_number"],
              nurse_action=f"{alert['alert_type']} alert done by {staff or 'unknown'}", details=note)


def count_open_alerts() -> int:
    init_db()
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'OPEN'").fetchone()[0]


def clear_all() -> None:
    """Delete all patients and alerts (demo mode's 'Reset demo data').

    The audit log is NEVER deleted – the reset itself is logged by the caller – and
    folder numbers are never reused.
    """
    init_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM patients")
        conn.execute("DELETE FROM alerts")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
AUDIT_COLUMNS = ["id", "timestamp", "event_type", "patient_id", "queue_number", "patient_name",
                 "input_text", "rules_result", "ai_output", "final_urgency", "nurse_action", "details",
                 "prev_hash", "row_hash"]


def add_audit(event_type: str, **fields) -> None:
    """Write one audit entry.

    For patient events, the patient's name, queue number and colour AT THIS MOMENT are
    filled in automatically, so every entry shows who it was about and how urgent they were.
    """
    init_db()
    fields = {k: v for k, v in fields.items() if k in AUDIT_COLUMNS}
    if fields.get("patient_id") is not None:
        patient = get_patient(fields["patient_id"])
        if patient:
            fields.setdefault("patient_name",
                              f"{patient['first_name']} {patient.get('surname') or ''}".strip())
            if fields.get("queue_number") is None:
                fields["queue_number"] = patient["queue_number"]
            if not fields.get("final_urgency"):
                fields["final_urgency"] = patient["nurse_urgency"] or patient["final_urgency"]
    if not fields.get("nurse_action") and event_type in DEFAULT_ACTIONS:
        fields["nurse_action"] = DEFAULT_ACTIONS[event_type]
    fields["timestamp"] = now_iso()
    fields["event_type"] = event_type
    with get_connection() as conn:
        # Lock while reading the last hash and appending, so the chain never forks.
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        fields["prev_hash"] = (last[0] if last else None) or GENESIS_HASH
        fields["row_hash"] = audit_hash(fields, fields["prev_hash"])
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        conn.execute(f"INSERT INTO audit_log ({columns}) VALUES ({placeholders})", tuple(fields.values()))


def verify_audit_chain() -> dict:
    """Re-calculate every hash in the audit log.

    Returns {"ok": bool, "entries": n, "first_bad_id": id or None}. Any edit to, or
    removal of, an earlier entry (e.g. directly in the database file) breaks the chain.
    """
    init_db()
    prev = GENESIS_HASH
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    for row in rows:
        entry = dict(row)
        if entry["prev_hash"] != prev or entry["row_hash"] != audit_hash(entry, prev):
            return {"ok": False, "entries": len(rows), "first_bad_id": entry["id"]}
        prev = entry["row_hash"]
    return {"ok": True, "entries": len(rows), "first_bad_id": None}


def get_audit_log(event_type: Optional[str] = None, patient_id: Optional[int] = None) -> list[dict]:
    init_db()
    sql, params = "SELECT * FROM audit_log WHERE 1 = 1", []
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    if patient_id is not None:
        sql += " AND patient_id = ?"
        params.append(patient_id)
    with get_connection() as conn:
        rows = conn.execute(sql + " ORDER BY id DESC", params).fetchall()
    return [dict(r) for r in rows]


def audit_log_csv(rows: Optional[list[dict]] = None) -> str:
    rows = rows if rows is not None else get_audit_log()
    return _write_csv(rows, AUDIT_COLUMNS)
