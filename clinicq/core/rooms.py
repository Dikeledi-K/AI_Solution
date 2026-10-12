"""The clinic's rooms and who is working in each one.

Rooms come from data/rooms.json (name, type, which stations may use it).
Staff never type a room: they pick one that is
    - meant for their station, and
    - FREE: nobody else is working in it and no patient is still inside.

A room is "claimed" while a staff member works in it. The claim is released
when they sign out or change room. As a safety net (for example the browser was
just closed), a claim also expires after CLAIM_MINUTES without any activity
from that staff member – but a room with a patient inside is never offered.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core import database as db

ROOMS_PATH = Path(__file__).resolve().parent.parent / "data" / "rooms.json"
CLAIM_MINUTES = int(os.getenv("CLINICQ_SESSION_MINUTES", "30"))

CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_claims (
    room        TEXT PRIMARY KEY,
    staff_id    TEXT NOT NULL,
    staff_name  TEXT NOT NULL,
    station     TEXT NOT NULL,
    claimed_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
"""


class RoomUnavailable(Exception):
    """The room cannot be taken. The message is safe to show to staff."""


def load_rooms() -> list[dict]:
    path = Path(os.getenv("CLINICQ_ROOMS_PATH", str(ROOMS_PATH)))
    with open(path, encoding="utf-8") as f:
        return json.load(f)["rooms"]


def room_names() -> list[str]:
    return [r["name"] for r in load_rooms()]


def rooms_for_station(station: str) -> list[str]:
    return [r["name"] for r in load_rooms() if station in r["stations"]]


def _init() -> None:
    db.init_db()
    with db.get_connection() as conn:
        conn.executescript(CLAIMS_SCHEMA)


def _cutoff(now: datetime) -> str:
    return (now - timedelta(minutes=CLAIM_MINUTES)).isoformat(timespec="seconds")


def active_claims(now: Optional[datetime] = None) -> dict[str, dict]:
    """{room: claim} for claims that have not expired."""
    _init()
    now = now or datetime.now()
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM room_claims WHERE last_seen >= ?", (_cutoff(now),)).fetchall()
    return {r["room"]: dict(r) for r in rows}


def _all_claims() -> dict[str, dict]:
    """{room: claim}, including expired claims (an expired claim still belongs to its
    owner until somebody else takes the room)."""
    _init()
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM room_claims").fetchall()
    return {r["room"]: dict(r) for r in rows}


def room_status(now: Optional[datetime] = None) -> list[dict]:
    """Every room with who is working there and which patient (if any) is inside."""
    claims = active_claims(now)
    patients = db.called_by_room()
    return [r | {"claim": claims.get(r["name"]), "patient": patients.get(r["name"])} for r in load_rooms()]


def available_rooms(station: str, staff_id: str, now: Optional[datetime] = None) -> list[str]:
    """Rooms this staff member may choose right now for this station."""
    active = active_claims(now)
    every = _all_claims()
    patients = db.called_by_room()
    free = []
    for name in rooms_for_station(station):
        mine = name in every and every[name]["staff_id"] == staff_id
        if name in active and not mine:
            continue  # someone else is working there
        if name in patients and not mine:
            continue  # a patient is still inside
        free.append(name)
    return free


def claim_room(room: str, staff_id: str, staff_name: str, station: str,
               now: Optional[datetime] = None) -> None:
    """Take a room. Atomic, so two staff can never take the same room at the same moment."""
    _init()
    now = now or datetime.now()
    if room not in rooms_for_station(station):
        raise RoomUnavailable(f"{room} is not a room for this station.")
    stamp = now.isoformat(timespec="seconds")
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")  # lock the database while we check and claim
        claim = conn.execute("SELECT * FROM room_claims WHERE room = ? AND last_seen >= ?",
                             (room, _cutoff(now))).fetchone()
        if claim and claim["staff_id"] != staff_id:
            raise RoomUnavailable(f"{room} has just been taken by {claim['staff_name']}. Please choose another room.")
        inside = conn.execute("SELECT queue_number FROM patients WHERE status = 'called' AND room = ?",
                              (room,)).fetchone()
        if inside and not claim:
            raise RoomUnavailable(f"Patient #{inside[0]} is still in {room}. Please choose another room.")
        conn.execute("DELETE FROM room_claims WHERE staff_id = ?", (staff_id,))  # one room per person
        conn.execute("INSERT OR REPLACE INTO room_claims VALUES (?, ?, ?, ?, ?, ?)",
                     (room, staff_id, staff_name, station, stamp, stamp))
    db.add_audit("ROOM", nurse_action=f"{staff_name} ({staff_id}) started working in {room}")


def my_claim(staff_id: str, now: Optional[datetime] = None) -> Optional[dict]:
    return next((c for c in active_claims(now).values() if c["staff_id"] == staff_id), None)


def heartbeat(staff_id: str, now: Optional[datetime] = None) -> bool:
    """Keep this staff member's room reserved. Returns False if they no longer have it.

    An expired claim is renewed as long as nobody else has taken the room in the
    meantime – so a nurse busy with a long consultation never loses her room.
    """
    if not any(c["staff_id"] == staff_id for c in _all_claims().values()):
        return False
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    with db.get_connection() as conn:
        conn.execute("UPDATE room_claims SET last_seen = ? WHERE staff_id = ?", (stamp, staff_id))
    return True


def release_room(staff_id: str) -> None:
    _init()
    with db.get_connection() as conn:
        rows = conn.execute("SELECT room, staff_name FROM room_claims WHERE staff_id = ?", (staff_id,)).fetchall()
        conn.execute("DELETE FROM room_claims WHERE staff_id = ?", (staff_id,))
    for r in rows:
        db.add_audit("ROOM", nurse_action=f"{r['staff_name']} ({staff_id}) left {r['room']}")


def admin_release(room: str, by: str) -> None:
    """Free a room that was left claimed (e.g. someone went home without signing out)."""
    _init()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM room_claims WHERE room = ?", (room,))
    db.add_audit("ROOM", nurse_action=f"{room} released by {by}")
