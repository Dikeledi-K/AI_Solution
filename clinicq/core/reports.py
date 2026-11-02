"""Daily report for the facility manager – numbers only, no names or ID numbers.

    - How many patients were registered, finished, referred, by triage colour.
    - Waiting times: from arrival until first called to Vitals, per colour, and the
      share seen within the SATS target time.
    - How the AI is doing: failures, and how often the vitals nurse changed the
      suggested colour – PER LANGUAGE. A pattern such as "the nurse often raises the
      colour for Sesotho entries" is the early warning sign of language bias that
      RESPONSIBLE_AI.md asks the clinic to watch for.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from statistics import median
from typing import Optional

from core import database as db
from core.models import Urgency
from core.queue_manager import TARGET_MINUTES, effective_urgency

COLOURS = ["RED", "ORANGE", "YELLOW", "GREEN"]


def _minutes(start: str, end: str) -> int:
    return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60))


def _first_vitals_call(audit: list[dict]) -> dict[int, str]:
    """{patient_id: time first called to the Vitals station}."""
    first: dict[int, str] = {}
    for e in sorted(audit, key=lambda e: e["id"]):
        if (e["event_type"] == "STATUS" and e["patient_id"] is not None
                and (e["nurse_action"] or "").startswith("Called to") and "(VITALS)" in e["nurse_action"]):
            first.setdefault(e["patient_id"], e["timestamp"])
    return first


def _language(patient: dict) -> str:
    llm = json.loads(patient["triage_json"]).get("llm_output")
    return (llm or {}).get("detected_language") or "unknown (AI failed)"


def daily_report(day: Optional[date] = None) -> dict:
    day = day or date.today()
    iso = day.isoformat()
    patients = [p for p in db.list_patients() if (p.get("visit_date") or p["arrival_time"][:10]) == iso]
    audit = db.get_audit_log()
    first_call = _first_vitals_call(audit)

    by_colour = {c: 0 for c in COLOURS}
    waits = {c: [] for c in COLOURS}
    within = {c: 0 for c in COLOURS}
    languages: dict[str, dict] = {}
    for p in patients:
        colour = effective_urgency(p).value
        by_colour[colour] += 1
        if p["id"] in first_call:
            minutes = _minutes(p["arrival_time"], first_call[p["id"]])
            waits[colour].append(minutes)
            within[colour] += minutes <= TARGET_MINUTES[Urgency(colour)]
        lang = languages.setdefault(_language(p), {"patients": 0, "reviewed": 0, "raised": 0, "lowered": 0})
        lang["patients"] += 1
        if p["nurse_reviewed"]:
            lang["reviewed"] += 1
            suggested = Urgency(p["final_urgency"]).rank
            confirmed = Urgency(p["nurse_urgency"]).rank
            lang["raised"] += confirmed > suggested
            lang["lowered"] += confirmed < suggested

    finished = [p for p in patients if p["status"] == "done"]
    in_clinic = [_minutes(p["arrival_time"], p["seen_time"]) for p in finished if p.get("seen_time")]
    ai_failed = sum(1 for p in patients if json.loads(p["triage_json"]).get("llm_output") is None)
    overdue_events = sum(1 for e in audit if e["event_type"] == "OVERDUE" and e["timestamp"].startswith(iso))

    return {
        "day": iso,
        "registered": len(patients),
        "finished": len(finished),
        "still_in_clinic": len(patients) - len(finished),
        "outcomes": {o: sum(p["outcome"] == o for p in finished) for o in ("DISCHARGED", "REFERRED", "DECEASED")},
        "by_colour": by_colour,
        "wait_to_vitals": {
            c: {"called": len(waits[c]),
                "median_minutes": int(median(waits[c])) if waits[c] else None,
                "within_target_pct": round(100 * within[c] / len(waits[c])) if waits[c] else None,
                "target_minutes": TARGET_MINUTES[Urgency(c)]}
            for c in COLOURS
        },
        "median_minutes_in_clinic": int(median(in_clinic)) if in_clinic else None,
        "overdue": overdue_events,
        "ai_failed": ai_failed,
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1]["patients"])),
    }


# ---------------------------------------------------------------------------
# Staff activity: how many patients each staff member helped, and when
# ---------------------------------------------------------------------------
# Audit events that mean "this staff member did something for this patient".
ASSIST_EVENTS = {
    "TRIAGE": "Registered", "NURSE_CONFIRM": "Confirmed colour", "NURSE_OVERRIDE": "Changed colour",
    "VITALS": "Vitals", "CLINICIAN": "Assessment & prescription", "HIV_TEST": "HIV test",
    "STATION": "Sent on", "STATUS": None, "REFERRED": "Referred to hospital", "DECEASED": "Death recorded",
    "ALERT": "Alert task",
}
_STAFF_ID = re.compile(r"\(([A-Z0-9]{3,12})\)\s*$")


def _action(entry: dict) -> str:
    name = ASSIST_EVENTS[entry["event_type"]]
    if name:
        return name
    text = entry["nurse_action"] or ""
    if text.startswith("Called to"):
        return "Called"
    if text.startswith("Not here"):
        return "Not here"
    if text.startswith("Visit ended"):
        return "Sent home" if "DISCHARGED" in text else "Visit ended"
    return "Status"


def staff_activity(day: Optional[date] = None) -> list[dict]:
    """One row per staff member who helped patients on `day`, busiest first.

    Each row: staff, staff_id, role, patients (number), events, first, last (HH:MM),
    signed_in, signed_out (first sign-in / last sign-out that day), avg_minutes per patient,
    and visits: one line per patient with what they did and from when to when.
    Time with a patient = from the first to the last thing this staff member did for them
    (e.g. called -> sent on); a single action (e.g. registration) has no duration.
    """
    from core import auth  # local import: auth imports database too

    iso = (day or date.today()).isoformat()
    entries = [e for e in db.get_audit_log() if e["timestamp"].startswith(iso) and e.get("staff")]
    people: dict[str, dict] = {}
    for e in sorted(entries, key=lambda e: e["id"]):
        who = e["staff"]
        person = people.setdefault(who, {"staff": who, "visits": {}, "events": 0,
                                          "signed_in": None, "signed_out": None})
        if e["event_type"] == "STAFF_LOGIN":
            text = e["nurse_action"] or ""
            if text.endswith("signed in") and person["signed_in"] is None:
                person["signed_in"] = e["timestamp"][11:16]
            elif "signed out" in text:
                person["signed_out"] = e["timestamp"][11:16]
            continue
        if e["event_type"] not in ASSIST_EVENTS or e["patient_id"] is None:
            continue
        person["events"] += 1
        visit = person["visits"].setdefault(e["patient_id"], {
            "patient_id": e["patient_id"], "queue_number": e["queue_number"],
            "patient_name": e.get("patient_name"), "start": e["timestamp"], "end": e["timestamp"], "actions": []})
        visit["end"] = e["timestamp"]
        action = _action(e)
        if action not in visit["actions"]:
            visit["actions"].append(action)

    rows = []
    for who, person in people.items():
        match = _STAFF_ID.search(who)
        if not person["visits"] or not match:  # skip e.g. the demo loader, which is not a person
            continue
        account = auth.get_staff(match.group(1)) if match else None
        visits = []
        for v in person["visits"].values():
            minutes = _minutes(v["start"], v["end"])
            visits.append({"queue_number": v["queue_number"], "patient_id": v["patient_id"],
                           "patient_name": v["patient_name"], "from": v["start"][11:16], "to": v["end"][11:16],
                           "minutes": minutes if v["start"] != v["end"] else None,
                           "did": ", ".join(v["actions"])})
        timed = [v["minutes"] for v in visits if v["minutes"] is not None]
        rows.append({
            "staff": who, "staff_id": match.group(1),
            "role": account["role"] if account else "—",
            "patients": len(visits), "events": person["events"],
            "first": min(v["from"] for v in visits), "last": max(v["to"] for v in visits),
            "signed_in": person["signed_in"], "signed_out": person["signed_out"],
            "avg_minutes": round(sum(timed) / len(timed)) if timed else None,
            "visits": sorted(visits, key=lambda v: v["from"]),
        })
    return sorted(rows, key=lambda r: (-r["patients"], r["staff"]))
