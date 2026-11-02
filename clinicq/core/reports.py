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
