"""Queue logic: ordering, positions, statistics and SMS triggers.

Ordering: RED -> ORANGE -> YELLOW -> GREEN, then earliest arrival first.
If a nurse has confirmed/overridden a patient, the NURSE's urgency is used.

Fairness for long waits: each colour has a target time (South African Triage
Scale). Anyone waiting longer than their target is flagged OVERDUE. An overdue
GREEN patient is also moved up to queue among the YELLOW patients by arrival
time, so routine patients are not pushed back all day. RED and ORANGE are
emergencies and are never jumped automatically; the colour label itself only
changes if a nurse overrides it.

Each station (Vitals, Nurse/Doctor, Pharmacy ...) has its own queue with the
same ordering. Overdue targets only apply while waiting for Vitals or the
Nurse/Doctor, because SATS targets are about the wait to be SEEN.

Privacy: each room can have only ONE called patient at a time.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from core import database as db
from core.models import Urgency
from core.stations import STATIONS, TRIAGE_STATIONS
from core.sms_service import SMSService, called_message, near_front_message

# Send the "you'll be called soon" SMS when this many (or fewer) people are ahead.
NEAR_FRONT_PLACES = 2

# Maximum wait (minutes) per colour, based on the South African Triage Scale.
TARGET_MINUTES = {Urgency.RED: 0, Urgency.ORANGE: 10, Urgency.YELLOW: 60, Urgency.GREEN: 240}


def effective_urgency(patient: dict) -> Urgency:
    """The urgency used for the queue: nurse decision if any, otherwise rules + AI."""
    return Urgency(patient["nurse_urgency"] or patient["final_urgency"])


def wait_minutes(patient: dict, now: datetime | None = None) -> int:
    """Minutes since arrival (or until called, if already called/seen)."""
    start = datetime.fromisoformat(patient["arrival_time"])
    end = patient.get("called_time")
    end_time = datetime.fromisoformat(end) if end else (now or datetime.now())
    return max(0, int((end_time - start).total_seconds() // 60))


def minutes_over_target(patient: dict, now: datetime | None = None) -> int:
    """How far past the colour's target time the patient is (negative = still within target)."""
    return wait_minutes(patient, now) - TARGET_MINUTES[effective_urgency(patient)]


def is_overdue(patient: dict, now: datetime | None = None) -> bool:
    return (patient["status"] == "waiting"
            and (patient.get("station") or "VITALS") in TRIAGE_STATIONS
            and minutes_over_target(patient, now) > 0)


def moved_up_for_waiting(patient: dict, now: datetime | None = None) -> bool:
    """True for a GREEN patient who has waited past target and now queues with YELLOW."""
    return effective_urgency(patient) == Urgency.GREEN and is_overdue(patient, now)


def queue_band(patient: dict, now: datetime | None = None) -> int:
    """The rank used for ordering. Same as the colour, except overdue GREEN sorts as YELLOW."""
    if moved_up_for_waiting(patient, now):
        return Urgency.YELLOW.rank
    return effective_urgency(patient).rank


def sort_key(patient: dict, now: datetime | None = None):
    return (-queue_band(patient, now), patient["arrival_time"], patient["queue_number"])


def get_waiting_queue(now: datetime | None = None, station: str | None = None) -> list[dict]:
    """Patients waiting (at one station, or anywhere), in the order they should be seen."""
    now = now or datetime.now()
    return sorted(db.list_patients(("waiting",), station=station), key=lambda p: sort_key(p, now))


def log_new_overdue(now: datetime | None = None) -> list[int]:
    """Write an OVERDUE audit entry the first time each waiting patient passes their target.

    Returns the queue numbers that were newly logged. If a nurse changes the colour,
    the flag is reset (see database.set_nurse_urgency), because the target changes too.
    """
    now = now or datetime.now()
    logged = []
    for p in get_waiting_queue(now):
        if p["overdue_logged"] or not is_overdue(p, now):
            continue
        urgency = effective_urgency(p)
        moved = " Moved up to queue with YELLOW." if moved_up_for_waiting(p, now) else ""
        db.add_audit(
            "OVERDUE",
            patient_id=p["id"],
            queue_number=p["queue_number"],
            final_urgency=urgency.value,
            details=f"Waited {wait_minutes(p, now)} min; {urgency.value} target is "
                    f"{TARGET_MINUTES[urgency]} min.{moved}",
        )
        db.mark_overdue_logged(p["id"])
        logged.append(p["queue_number"])
    return logged


def get_called() -> list[dict]:
    return sorted(db.list_patients(("called",)), key=sort_key)


def notify_near_front(sms: SMSService) -> list[str]:
    """SMS anyone with NEAR_FRONT_PLACES or fewer people ahead at their station (once per station)."""
    sent = []
    for station in STATIONS:
        sent += _notify_station(sms, station)
    return sent


def _notify_station(sms: SMSService, station: str) -> list[str]:
    sent = []
    for position, patient in enumerate(get_waiting_queue(station=station)):  # 0 = next in line
        if position > NEAR_FRONT_PLACES:
            break
        if patient["phone"] and not patient["near_front_sms_sent"]:
            result = sms.send(patient["phone"], near_front_message(patient["queue_number"]))
            db.mark_near_front_sms_sent(patient["id"])
            db.add_audit("SMS", patient_id=patient["id"], queue_number=patient["queue_number"],
                         details=f"near-front SMS to {result.to_masked}: "
                                 f"{'sent' if result.success else result.error}")
            sent.append(result.to_masked)
    return sent


class CallError(Exception):
    """A patient could not be called. The message is shown to the nurse."""


class RoomBusyError(CallError):
    """The nurse's room already has a called patient."""


class AlreadyCalledError(CallError):
    """Another nurse called this patient a moment earlier."""


def call_patient(patient_id: int, sms: SMSService, room: str = db.DEFAULT_ROOM,
                 staff: str | None = None) -> None:
    """Call a patient into a room, SMS them, then check who is now near the front.

    Privacy: a room can only have ONE called patient at a time. The previous
    patient must be marked as seen (or 'not here') before the next is called.
    Raises AlreadyCalledError or RoomBusyError if the call is not possible.
    """
    # Room names come from data/rooms.json and are used exactly as written
    # (e.g. "HIV Counselling Room 1", "Emergency Room 1 (Resus)").
    if not db.call_to_room(patient_id, room, staff):
        patient = db.get_patient(patient_id)
        if patient["status"] != "waiting":
            where = f" to {patient['room']}" if patient["status"] == "called" and patient["room"] else ""
            raise AlreadyCalledError(f"#{patient['queue_number']} was just called{where} "
                                     "by another staff member.")
        current = db.patient_in_room(room)
        who = f"#{current['queue_number']}" if current else "another patient"
        raise RoomBusyError(f"{room} is still busy with {who}. Finish with them "
                            "(or press 'Not here') before calling the next patient.")
    patient = db.get_patient(patient_id)
    if patient["phone"]:
        # Privacy: the SMS names the room only – never the station (e.g. "HIV testing").
        result = sms.send(patient["phone"], called_message(patient["queue_number"], room))
        db.add_audit("SMS", patient_id=patient_id, queue_number=patient["queue_number"],
                     details=f"called SMS to {result.to_masked}: "
                             f"{'sent' if result.success else result.error}")
    notify_near_front(sms)


def not_here(patient_id: int) -> None:
    """Called patient did not come: free the room and put them back in the queue."""
    db.return_to_queue(patient_id)


def queue_stats() -> dict:
    """Simple numbers for the dashboard header."""
    waiting = get_waiting_queue()
    per_colour = {u.value: 0 for u in Urgency}
    for p in waiting:
        per_colour[effective_urgency(p).value] += 1
    waits = [wait_minutes(p) for p in waiting]
    return {
        "waiting_total": len(waiting),
        "per_colour": per_colour,
        "avg_wait_min": round(sum(waits) / len(waits), 1) if waits else 0,
        "awaiting_review": sum(1 for p in waiting if not p["nurse_reviewed"]),
        "overdue": sum(1 for p in waiting if is_overdue(p)),
        "per_station": {s: sum(1 for p in waiting if p["station"] == s) for s in STATIONS},
    }
