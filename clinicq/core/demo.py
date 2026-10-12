"""Load the 10 demo patients from data/sample_patients.json into the queue."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from core import database as db
from core.llm_client import LLMClient
from core.models import PatientIntake
from core.queue_manager import notify_near_front
from core.sms_service import SMSService
from core.triage import run_triage

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_patients.json"


def load_sample_cases() -> list[dict]:
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_demo_patients(sms: SMSService, client: LLMClient | None = None) -> int:
    """Triage every sample patient and add them to the queue. Returns how many were added.

    Arrival times are spread over the last hour so the wait times look realistic.
    """
    now = datetime.now()
    count = 0
    for case in load_sample_cases():
        intake = PatientIntake(**case["intake"])
        triage = run_triage(intake, client=client)
        arrival = (now - timedelta(minutes=case["minutes_ago"])).isoformat(timespec="seconds")
        db.add_patient(intake, triage, arrival_time=arrival)
        count += 1
    notify_near_front(sms)
    return count
