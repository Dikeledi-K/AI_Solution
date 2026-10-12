"""What happens at each station, and the alerts for referrals and deaths.

    record_vitals()        Vitals station: save BP/temp/urine tests, confirm colour -> Nurse/Doctor
    complete_clinician()   Nurse/Doctor: save prescription, choose next stops (or home)
    complete_station()     Pharmacy / Injection / HIV testing: done -> next stop (or home)
    refer_to_hospital()    alerts the ambulance coordinator
    ambulance_called()     coordinator records the ambulance -> alerts the receiving hospital
    record_death()         alerts the facility manager and creates follow-up tasks

All alerts are stored in the alerts table (shown on the Alerts page) and
"sent" with the SMS service, which is simulated in this prototype.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from core import database as db
from core.models import Urgency, Vitals, max_urgency
from core.privacy import mask_id
from core.queue_manager import effective_urgency, notify_near_front
from core.rules_engine import check_red_flags_for
from core.sms_service import SMSService
from core.stations import AFTER_CLINICIAN, STATIONS
from core.vitals import check_vitals, flags_urgency

CONTACTS_PATH = Path(__file__).resolve().parent.parent / "data" / "contacts.json"


def load_contacts() -> dict:
    path = Path(os.getenv("CLINICQ_CONTACTS_PATH", str(CONTACTS_PATH)))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _name(p: dict) -> str:
    return f"{p['first_name']} {p.get('surname') or ''}".strip()


# ---------------------------------------------------------------------------
# Vitals station
# ---------------------------------------------------------------------------
def suggest_after_vitals(patient: dict, vitals: Vitals) -> tuple[Urgency, list, bool]:
    """Colour suggested after vitals: the highest of current colour, vital-sign flags and
    the red-flag rules (re-run, because a positive pregnancy test can trigger pregnancy rules).

    Returns (suggested colour, vitals flags, pregnant?).
    """
    pregnant = patient["pregnant"] == "Yes" or vitals.urine_pregnancy == "Positive"
    flags = check_vitals(vitals, pregnant)
    suggested = effective_urgency(patient)
    for level in (flags_urgency(flags),
                  check_red_flags_for(patient["symptoms_text"], patient["age"], pregnant).urgency):
        if level is not None:
            suggested = max_urgency(suggested, level)
    return suggested, flags, pregnant


def record_vitals(patient_id: int, vitals: Vitals, staff: str, sms: SMSService,
                  chosen: Optional[Urgency] = None, reason: Optional[str] = None,
                  note: str = "") -> dict:
    """Save vitals, confirm the colour and send the patient to the Nurse/Doctor queue.

    chosen = the colour the nurse picked (None = accept the suggestion). Choosing a
    LOWER colour than suggested needs a reason, like any other override.
    note   = the vitals nurse's note for the Nurse/Doctor who will prescribe.
    """
    patient = db.get_patient(patient_id)
    suggested, flags, pregnant = suggest_after_vitals(patient, vitals)
    final = chosen or suggested
    if final.rank < suggested.rank and not (reason or "").strip():
        raise ValueError(f"Please give a reason for choosing {final.value} when {suggested.value} "
                         "is suggested.")

    record = vitals.model_dump() | {"flags": [f.model_dump(mode="json") for f in flags],
                                    "suggested": suggested.value, "note": note.strip()}
    newly_pregnant = "Yes" if vitals.urine_pregnancy == "Positive" else None
    summary = vitals.summary() + (f". Note: {note.strip()}" if note.strip() else "")
    db.save_vitals(patient_id, json.dumps(record), staff, newly_pregnant, summary)

    if not reason and flags:
        reason = "Vitals: " + "; ".join(f.name for f in flags)
    db.set_nurse_urgency(patient_id, final, reason or None,
                         override=final.value != patient["final_urgency"], staff=staff)
    db.move_to_station(patient_id, "CLINICIAN", [], staff)
    notify_near_front(sms)
    return {"suggested": suggested, "final": final, "flags": flags, "pregnant": pregnant}


# ---------------------------------------------------------------------------
# Nurse / Doctor
# ---------------------------------------------------------------------------
def complete_clinician(patient_id: int, notes: str, next_stops: list[str], staff: str,
                       sms: SMSService) -> Optional[str]:
    """Save the assessment/prescription and send the patient on (or home).

    The chosen rooms are always visited in the fixed clinic order
    HIV testing -> Injection room -> Pharmacy, whatever order they were ticked in.
    Returns the next station, or None if the patient goes home.
    """
    unknown = [s for s in next_stops if s not in AFTER_CLINICIAN]
    if unknown:
        raise ValueError(f"Unknown station(s): {unknown}")
    next_stops = [s for s in AFTER_CLINICIAN if s in next_stops]
    db.save_clinician_notes(patient_id, notes.strip(), staff)
    if next_stops:
        db.move_to_station(patient_id, next_stops[0], next_stops[1:], staff)
        notify_near_front(sms)
        return next_stops[0]
    db.finish_visit(patient_id, "DISCHARGED", staff=staff)
    notify_near_front(sms)
    return None


# ---------------------------------------------------------------------------
# Pharmacy / Injection room / HIV testing
# ---------------------------------------------------------------------------
def complete_station(patient_id: int, staff: str, sms: SMSService, note: str = "") -> Optional[str]:
    """Done at this station: go to the next stop the Nurse/Doctor chose, or home."""
    patient = db.get_patient(patient_id)
    pending = json.loads(patient["pending_stations"] or "[]")
    if pending:
        db.move_to_station(patient_id, pending[0], pending[1:], staff, note)
        notify_near_front(sms)
        return pending[0]
    db.finish_visit(patient_id, "DISCHARGED", {"note": note} if note else None, staff)
    notify_near_front(sms)
    return None


# ---------------------------------------------------------------------------
# HIV testing room
# ---------------------------------------------------------------------------
HIV_RESULTS = ["Negative", "Positive (confirmed with a second test)", "Inconclusive / discordant",
               "Patient declined testing"]


def record_hiv_test(patient_id: int, result: str, consented: bool, staff: str, sms: SMSService,
                    back_to_clinician: bool = False, note: str = "") -> Optional[str]:
    """Record an HIV test, then send the patient on.

    - Testing needs pre-test counselling and the patient's consent (unless they declined).
    - A POSITIVE result always goes back to the Nurse/Doctor for ART initiation and
      linkage to care – it cannot just continue to the pharmacy.
    Returns the next station, or None if the patient goes home.
    """
    if result not in HIV_RESULTS:
        raise ValueError("Please choose the test result.")
    declined = result == "Patient declined testing"
    if not declined and not consented:
        raise ValueError("Pre-test counselling and the patient's consent must be recorded before testing.")
    positive = result.startswith("Positive")
    if positive and not back_to_clinician:
        raise ValueError("A positive result must go back to the Nurse/Doctor for ART initiation "
                         "and linkage to care.")

    record = {"result": result, "consented": consented, "tested_by": staff,
              "time": db.now_iso(), "note": note.strip()}
    db.save_hiv_test(patient_id, json.dumps(record), staff)
    if back_to_clinician:
        # Keep the remaining rooms (e.g. pharmacy) so the patient still gets them afterwards.
        # The audit note is deliberately neutral: it must never reveal the result.
        patient = db.get_patient(patient_id)
        pending = json.loads(patient["pending_stations"] or "[]")
        db.move_to_station(patient_id, "CLINICIAN", pending, staff, "Sent back after HIV testing")
        notify_near_front(sms)
        return "CLINICIAN"
    return complete_station(patient_id, staff, sms)


def send_back_to_clinician(patient_id: int, staff: str, sms: SMSService, note: str) -> None:
    """E.g. a test result needs the Nurse/Doctor again. Remaining stops are cleared –
    the Nurse/Doctor chooses them again."""
    db.move_to_station(patient_id, "CLINICIAN", [], staff, f"Sent back: {note}")
    notify_near_front(sms)


# ---------------------------------------------------------------------------
# Hospital referral
# ---------------------------------------------------------------------------
def refer_to_hospital(patient_id: int, hospital: str, reason: str, staff: str,
                      sms: SMSService) -> int:
    """End the visit as REFERRED and alert the ambulance coordinator. Returns the alert id."""
    if not reason.strip():
        raise ValueError("Please give the reason for the referral.")
    contacts = load_contacts()
    patient = db.get_patient(patient_id)
    colour = effective_urgency(patient).value
    db.finish_visit(patient_id, "REFERRED",
                    {"hospital": hospital, "reason": reason.strip(), "referred_by": staff}, staff)
    coordinator = contacts["ambulance_coordinator"]
    alert_id = db.add_alert(
        "AMBULANCE", coordinator["name"],
        f"Ambulance needed for #{patient['queue_number']} {_name(patient)} ({colour}) – transfer to "
        f"{hospital}. Reason: {reason.strip()}. Requested by {staff}. Call "
        f"{contacts.get('emergency_services', '10177')}, then record the reference number here.",
        patient,
    )
    # SMS to staff: queue number and colour only, no name or diagnosis.
    sms.send(coordinator["phone"], f"ClinicQ ALERT: ambulance needed for queue no. "
                                   f"{patient['queue_number']} ({colour}). Open ClinicQ Alerts.")
    notify_near_front(sms)
    return alert_id


def referral_summary(patient: dict, reference: str, eta_minutes: int) -> str:
    """Handover note for the receiving hospital."""
    details = json.loads(patient["outcome_details"] or "{}")
    vitals = json.loads(patient["vitals_json"]) if patient.get("vitals_json") else None
    vitals_text = (f"BP {vitals['bp_systolic']}/{vitals['bp_diastolic']}, temp {vitals['temperature']}°C"
                   + (f", pulse {vitals['pulse']}" if vitals.get("pulse") else "")) if vitals else "not taken"
    return (
        f"Incoming patient: {_name(patient)}, {patient['age']}y {patient['sex']}, "
        f"ID {mask_id(patient.get('id_number'))}. Triage: {effective_urgency(patient).value}. "
        f"Complaint: {patient['chief_complaint']}. Vitals: {vitals_text}. "
        f"Clinic notes: {patient.get('clinician_notes') or '—'}. Reason for referral: "
        f"{details.get('reason', '—')}. Referred by {details.get('referred_by', '—')}. "
        f"Ambulance ref {reference}, ETA about {eta_minutes} min."
    )


def ambulance_called(alert_id: int, reference: str, eta_minutes: int, staff: str,
                     sms: SMSService) -> int:
    """Coordinator has called the ambulance: close that alert and alert the hospital.

    Returns the id of the new HOSPITAL alert.
    """
    if not reference.strip():
        raise ValueError("Please enter the ambulance reference number.")
    alert = db.get_alert(alert_id)
    patient = db.get_patient(alert["patient_id"])
    details = json.loads(patient["outcome_details"] or "{}")
    hospital_name = details.get("hospital", "Hospital")
    hospital = next((h for h in load_contacts()["hospitals"] if h["name"] == hospital_name),
                    {"name": hospital_name, "phone": None})

    db.close_alert(alert_id, staff, f"Ambulance called. Ref {reference.strip()}, ETA {eta_minutes} min.")
    new_id = db.add_alert("HOSPITAL", hospital["name"],
                          referral_summary(patient, reference.strip(), eta_minutes), patient)
    if hospital["phone"]:
        # The SMS only announces the referral; the full handover note goes through a secure channel.
        sms.send(hospital["phone"], f"ClinicQ: incoming referral from the clinic (ambulance ref "
                                    f"{reference.strip()}), ETA about {eta_minutes} min.")
    return new_id


# ---------------------------------------------------------------------------
# Death
# ---------------------------------------------------------------------------
def record_death(patient_id: int, time_of_death: str, unnatural: bool, staff: str,
                 sms: SMSService) -> list[int]:
    """End the visit as DECEASED and alert the right people. Returns the alert ids.

    Next of kin are NOT sent an SMS: news of a death must be given by a person,
    in person or by phone. ClinicQ creates a task for that instead.
    """
    contacts = load_contacts()
    patient = db.get_patient(patient_id)
    db.finish_visit(patient_id, "DECEASED",
                    {"time_of_death": time_of_death, "unnatural": unnatural, "recorded_by": staff}, staff)
    manager = contacts["facility_manager"]
    ids = [db.add_alert(
        "DEATH_MANAGER", manager["name"],
        f"Death recorded: #{patient['queue_number']} {_name(patient)}, {patient['age']}y, "
        f"at {time_of_death}. Recorded by {staff}.", patient)]
    sms.send(manager["phone"], f"ClinicQ ALERT: a patient death has been recorded (queue no. "
                               f"{patient['queue_number']}). Please attend and open ClinicQ Alerts.")
    ids.append(db.add_alert(
        "DEATH_CERTIFY", "Doctor on duty",
        f"The death of #{patient['queue_number']} {_name(patient)} must be certified by a doctor.", patient))
    ids.append(db.add_alert(
        "NOK_CONTACT", "Facility manager / clinician",
        f"Inform the next of kin IN PERSON or BY PHONE (not by SMS): {patient.get('nok_name') or '—'} "
        f"({patient.get('nok_relationship') or '—'}), {patient.get('nok_phone') or '—'}.", patient))
    if unnatural:
        ids.append(db.add_alert(
            "SAPS", manager["name"],
            f"Unnatural or suspicious death of #{patient['queue_number']}: report it to SAPS (police) "
            "as required by law.", patient))
    notify_near_front(sms)
    return ids


ALERT_TYPES = {
    "AMBULANCE": "🚑 Ambulance needed",
    "HOSPITAL": "🏥 Notify receiving hospital",
    "DEATH_MANAGER": "🕊️ Death recorded",
    "DEATH_CERTIFY": "🩺 Certify death",
    "NOK_CONTACT": "📞 Inform next of kin",
    "SAPS": "👮 Report to SAPS",
}

STATION_LABELS = {k: v["label"] for k, v in STATIONS.items()}
