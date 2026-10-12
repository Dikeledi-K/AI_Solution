"""Tests for named rooms, room availability and the vitals nurse's note."""

import json
from datetime import datetime, timedelta

import pytest

from core import database as db
from core import rooms
from core.llm_client import MockLLMClient
from core.models import Vitals
from core.queue_manager import call_patient
from core.sms_service import MockSMSService
from core.triage import run_triage
from core.workflow import record_vitals

NOW = datetime(2026, 1, 1, 9, 0)


@pytest.fixture
def sms(tmp_path):
    return MockSMSService(tmp_path / "sms.log")


def register(make_intake, text="I need my repeat medication"):
    intake = make_intake(text)
    return db.add_patient(intake, run_triage(intake, client=MockLLMClient()))


def test_at_least_ten_named_rooms_including_emergency():
    all_rooms = rooms.load_rooms()
    assert len(all_rooms) >= 10
    names = [r["name"] for r in all_rooms]
    assert len(names) == len(set(names))  # no duplicates
    assert any(r["type"] == "Emergency" for r in all_rooms)
    assert any(n.startswith("Consultation Room") for n in names)


def test_rooms_are_offered_per_station():
    clinician = rooms.rooms_for_station("CLINICIAN")
    assert "Consultation Room 1" in clinician and "Emergency Room 1 (Resus)" in clinician
    assert "Pharmacy Counter 1" not in clinician
    assert rooms.rooms_for_station("PHARMACY") == ["Pharmacy Counter 1", "Pharmacy Counter 2"]


def test_a_taken_room_is_not_offered_to_others():
    rooms.claim_room("Consultation Room 1", "D2001", "Dr Naidoo", "CLINICIAN", now=NOW)
    assert "Consultation Room 1" not in rooms.available_rooms("CLINICIAN", "N1001", now=NOW)
    assert "Consultation Room 1" in rooms.available_rooms("CLINICIAN", "D2001", now=NOW)  # still mine


def test_two_people_cannot_take_the_same_room():
    rooms.claim_room("Consultation Room 2", "D2001", "Dr Naidoo", "CLINICIAN", now=NOW)
    with pytest.raises(rooms.RoomUnavailable, match="taken by Dr Naidoo"):
        rooms.claim_room("Consultation Room 2", "N1001", "Sr Dlamini", "CLINICIAN", now=NOW)


def test_room_must_belong_to_the_station():
    with pytest.raises(rooms.RoomUnavailable, match="not a room for this station"):
        rooms.claim_room("Pharmacy Counter 1", "N1001", "Sr Dlamini", "VITALS", now=NOW)


def test_one_room_per_person():
    rooms.claim_room("Vitals Room 1", "N1001", "Sr Dlamini", "VITALS", now=NOW)
    rooms.claim_room("Vitals Room 2", "N1001", "Sr Dlamini", "VITALS", now=NOW)
    claims = rooms.active_claims(NOW)
    assert "Vitals Room 1" not in claims and claims["Vitals Room 2"]["staff_id"] == "N1001"


def test_signing_out_frees_the_room():
    rooms.claim_room("Vitals Room 1", "N1001", "Sr Dlamini", "VITALS", now=NOW)
    rooms.release_room("N1001")
    assert "Vitals Room 1" in rooms.available_rooms("VITALS", "N2002", now=NOW)


def test_abandoned_room_frees_up_but_owner_keeps_it_until_someone_takes_it():
    rooms.claim_room("Vitals Room 1", "N1001", "Sr Dlamini", "VITALS", now=NOW)
    later = NOW + timedelta(minutes=rooms.CLAIM_MINUTES + 5)
    assert "Vitals Room 1" in rooms.available_rooms("VITALS", "N2002", now=later)
    assert rooms.heartbeat("N1001", now=later)  # owner comes back first: still hers
    rooms.claim_room("Vitals Room 2", "N2002", "Sr Mokoena", "VITALS", now=later)
    rooms.claim_room("Vitals Room 1", "N3003", "Sr Khumalo", "VITALS",
                     now=later + timedelta(minutes=rooms.CLAIM_MINUTES + 5))
    assert not rooms.heartbeat("N1001")  # someone else took it after it lapsed


def test_room_with_a_patient_inside_is_never_offered(make_intake, sms):
    pid = register(make_intake)
    rooms.claim_room("Vitals Room 1", "N1001", "Sr Dlamini", "VITALS")
    call_patient(pid, sms, room="Vitals Room 1", staff="Sr Dlamini (N1001)")
    rooms.admin_release("Vitals Room 1", "Clinic Admin (ADMIN)")  # nurse's claim is gone ...
    assert "Vitals Room 1" not in rooms.available_rooms("VITALS", "N2002")  # ... but patient is inside
    with pytest.raises(rooms.RoomUnavailable, match="still in Vitals Room 1"):
        rooms.claim_room("Vitals Room 1", "N2002", "Sr Mokoena", "VITALS")


def test_room_status_shows_staff_and_patient(make_intake, sms):
    pid = register(make_intake)
    rooms.claim_room("Vitals Room 2", "N1001", "Sr Dlamini", "VITALS")
    call_patient(pid, sms, room="Vitals Room 2", staff="Sr Dlamini (N1001)")
    status = {r["name"]: r for r in rooms.room_status()}
    assert status["Vitals Room 2"]["claim"]["staff_name"] == "Sr Dlamini"
    assert status["Vitals Room 2"]["patient"]["id"] == pid
    assert status["Injection Room"]["claim"] is None


def test_vitals_note_reaches_the_prescriber(make_intake, sms):
    pid = register(make_intake)
    note = "Patient anxious; missed BP tablets for 3 days."
    record_vitals(pid, Vitals(bp_systolic=150, bp_diastolic=92, temperature=36.9), "Sr Dlamini (N1001)", sms,
                  note=note)
    patient = db.get_patient(pid)
    assert patient["station"] == "CLINICIAN"
    assert json.loads(patient["vitals_json"])["note"] == note
    assert note in db.get_audit_log("VITALS")[0]["details"]
