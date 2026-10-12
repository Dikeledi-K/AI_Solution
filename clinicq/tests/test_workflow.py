"""Tests for patient details, vitals, the station journey, referrals and deaths."""

import json

import pytest
from pydantic import ValidationError

from core import database as db
from core.llm_client import MockLLMClient
from core.models import Urgency, Vitals
from core.privacy import luhn_valid, mask_id, mask_phone
from core.queue_manager import call_patient
from core.sms_service import MockSMSService
from core.triage import build_user_message, run_triage
from core.vitals import check_vitals
from core.workflow import (ambulance_called, complete_clinician, complete_station, record_death,
                           record_vitals, refer_to_hospital, send_back_to_clinician)


def make_sa_id(dob: str = "900101", sequence: str = "5009", citizen: str = "0") -> str:
    """Build a checksum-valid SA ID for tests (YYMMDD SSSS C 8 Z)."""
    base = dob + sequence + citizen + "8"
    for check in "0123456789":
        if luhn_valid(base + check):
            return base + check
    raise AssertionError("no check digit found")


@pytest.fixture
def sms(tmp_path):
    return MockSMSService(tmp_path / "sms.log")


def register(make_intake, text="I need my repeat medication", **kw):
    intake = make_intake(text, **kw)
    return db.add_patient(intake, run_triage(intake, client=MockLLMClient()))


NORMAL = dict(bp_systolic=120, bp_diastolic=80, temperature=36.8, pulse=78)


# ---------------------------------------------------------------------------
# Patient details
# ---------------------------------------------------------------------------
def test_valid_sa_id_is_accepted_and_birth_date_stored(make_intake):
    sa_id = make_sa_id("900101")
    from core.privacy import age_on, sa_id_birth_date
    age = age_on(sa_id_birth_date(sa_id))
    pid = register(make_intake, id_type="SA ID", id_number=sa_id, age=age)
    patient = db.get_patient(pid)
    assert patient["id_number"] == sa_id
    assert patient["date_of_birth"] == "1990-01-01"


def test_sa_id_with_bad_checksum_is_rejected(make_intake):
    bad = make_sa_id()[:-1] + str((int(make_sa_id()[-1]) + 1) % 10)
    with pytest.raises(ValidationError, match="checksum"):
        make_intake("cough", id_type="SA ID", id_number=bad)


def test_age_must_match_sa_id(make_intake):
    with pytest.raises(ValidationError, match="does not match"):
        make_intake("cough", id_type="SA ID", id_number=make_sa_id("900101"), age=20)


def test_id_number_required_unless_no_document(make_intake):
    with pytest.raises(ValidationError, match="Passport number"):
        make_intake("cough", id_type="Passport", id_number="")
    assert make_intake("cough", id_type="No document", id_number="123").id_number is None


def test_consent_and_next_of_kin_are_required(make_intake):
    with pytest.raises(ValidationError, match="must agree"):
        make_intake("cough", consent=False)
    with pytest.raises(ValidationError):
        make_intake("cough", nok_phone="")


def test_personal_details_are_never_sent_to_the_ai(make_intake):
    intake = make_intake("I have a cough", first_name="Lerato", surname="Mokoena",
                         id_type="Passport", id_number="DEMO77777", address="12 Secret Street",
                         nok_name="Mpho Mokoena", phone="0821234567")
    message = build_user_message(intake)
    for private in ["Lerato", "Mokoena", "DEMO77777", "Secret Street", "Mpho", "0821234567"]:
        assert private not in message


def test_mask_id():
    assert mask_id("9001015009087") == "*********9087"
    assert mask_id(None) == "—"


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------
def test_normal_vitals_raise_no_flags():
    assert check_vitals(Vitals(**NORMAL), pregnant=False) == []


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"bp_systolic": 85, "bp_diastolic": 50}, Urgency.RED),
        ({"bp_systolic": 190, "bp_diastolic": 100}, Urgency.ORANGE),
        ({"temperature": 39.4}, Urgency.ORANGE),
        ({"temperature": 40.2}, Urgency.RED),
        ({"pulse": 140}, Urgency.RED),
    ],
)
def test_dangerous_vitals_are_flagged(changes, expected):
    flags = check_vitals(Vitals(**(NORMAL | changes)), pregnant=False)
    assert flags and max(f.urgency.rank for f in flags) == expected.rank


def test_high_bp_matters_more_in_pregnancy():
    vitals = Vitals(**(NORMAL | {"bp_systolic": 150, "bp_diastolic": 95}))
    assert check_vitals(vitals, pregnant=False) == []
    assert check_vitals(vitals, pregnant=True)[0].urgency == Urgency.ORANGE


def test_vitals_raise_colour_and_move_to_clinician(make_intake, sms):
    pid = register(make_intake)  # GREEN
    result = record_vitals(pid, Vitals(**(NORMAL | {"temperature": 39.5})), "Sr Dlamini", sms)
    patient = db.get_patient(pid)
    assert result["final"] == Urgency.ORANGE
    assert patient["nurse_urgency"] == "ORANGE" and patient["nurse_reviewed"] == 1
    assert patient["station"] == "CLINICIAN" and patient["status"] == "waiting"
    assert patient["vitals_by"] == "Sr Dlamini"
    assert "High temperature" in patient["override_reason"]


def test_lowering_colour_after_vitals_needs_a_reason(make_intake, sms):
    pid = register(make_intake)
    hot = Vitals(**(NORMAL | {"temperature": 39.5}))
    with pytest.raises(ValueError, match="reason"):
        record_vitals(pid, hot, "Sr Dlamini", sms, chosen=Urgency.GREEN)
    record_vitals(pid, hot, "Sr Dlamini", sms, chosen=Urgency.GREEN, reason="Took paracetamol, rechecked")
    assert db.get_patient(pid)["nurse_urgency"] == "GREEN"


def test_positive_pregnancy_test_rechecks_pregnancy_danger_signs(make_intake, sms):
    pid = register(make_intake, "I have a severe headache and blurred vision", pregnant="N/A")
    assert db.get_patient(pid)["final_urgency"] != "RED"  # not known to be pregnant yet
    result = record_vitals(pid, Vitals(**(NORMAL | {"urine_pregnancy": "Positive"})), "Sr Dlamini", sms)
    assert result["final"] == Urgency.RED
    assert db.get_patient(pid)["pregnant"] == "Yes"


# ---------------------------------------------------------------------------
# Journey through the stations
# ---------------------------------------------------------------------------
def test_full_journey_through_stations(make_intake, sms):
    pid = register(make_intake)
    record_vitals(pid, Vitals(**NORMAL), "Sr Dlamini", sms)
    nxt = complete_clinician(pid, "Amoxicillin 500mg tds x 5 days", ["HIV_TEST", "PHARMACY"],
                             "Dr Naidoo", sms)
    patient = db.get_patient(pid)
    assert nxt == "HIV_TEST" and json.loads(patient["pending_stations"]) == ["PHARMACY"]
    assert patient["clinician_name"] == "Dr Naidoo"
    assert complete_station(pid, "Counsellor Zulu", sms) == "PHARMACY"
    assert complete_station(pid, "Pharmacist Botha", sms) is None
    patient = db.get_patient(pid)
    assert patient["status"] == "done" and patient["outcome"] == "DISCHARGED"


def test_clinician_can_send_patient_straight_home(make_intake, sms):
    pid = register(make_intake)
    record_vitals(pid, Vitals(**NORMAL), "Sr Dlamini", sms)
    assert complete_clinician(pid, "Advice only", [], "Dr Naidoo", sms) is None
    assert db.get_patient(pid)["outcome"] == "DISCHARGED"


def test_station_can_send_patient_back_to_clinician(make_intake, sms):
    pid = register(make_intake)
    record_vitals(pid, Vitals(**NORMAL), "Sr Dlamini", sms)
    complete_clinician(pid, "Test first", ["HIV_TEST", "PHARMACY"], "Dr Naidoo", sms)
    send_back_to_clinician(pid, "Counsellor Zulu", sms, "Result needs discussion")
    patient = db.get_patient(pid)
    assert patient["station"] == "CLINICIAN" and json.loads(patient["pending_stations"]) == []


def test_called_sms_never_names_the_station(make_intake, sms):
    pid = register(make_intake, phone="0821234567")
    record_vitals(pid, Vitals(**NORMAL), "Sr Dlamini", sms)
    complete_clinician(pid, "Test", ["HIV_TEST"], "Dr Naidoo", sms)
    call_patient(pid, sms, room="Room 5", staff="Counsellor Zulu")
    called = [m for m in sms.recent_messages(50) if "please come to" in m]
    assert called and "Room 5" in called[0]
    assert not any("HIV" in m for m in sms.recent_messages(50))


# ---------------------------------------------------------------------------
# Hospital referral
# ---------------------------------------------------------------------------
def test_referral_alerts_ambulance_then_hospital(make_intake, sms):
    pid = register(make_intake, "I have chest pain", first_name="Thabo", surname="Mahlangu",
                   id_type="Passport", id_number="DEMO10010")
    alert_id = refer_to_hospital(pid, "District Hospital (example)", "Suspected cardiac event",
                                 "Dr Naidoo", sms)
    patient = db.get_patient(pid)
    assert patient["status"] == "done" and patient["outcome"] == "REFERRED"
    ambulance = db.get_alert(alert_id)
    assert ambulance["alert_type"] == "AMBULANCE" and ambulance["status"] == "OPEN"
    # SMS to the coordinator has no patient name
    assert any("ambulance needed" in m for m in sms.recent_messages())
    assert not any("Mahlangu" in m for m in sms.recent_messages())

    hospital_id = ambulance_called(alert_id, "EMS-4471", 25, "Coordinator Sibiya", sms)
    assert db.get_alert(alert_id)["status"] == "DONE"
    hospital = db.get_alert(hospital_id)
    assert hospital["alert_type"] == "HOSPITAL" and hospital["recipient"] == "District Hospital (example)"
    assert "Thabo Mahlangu" in hospital["message"] and "EMS-4471" in hospital["message"]
    assert "DEMO10010" not in hospital["message"]  # ID masked
    assert any("incoming referral" in m for m in sms.recent_messages())


def test_ambulance_reference_required(make_intake, sms):
    pid = register(make_intake, "I have chest pain")
    alert_id = refer_to_hospital(pid, "District Hospital (example)", "Chest pain", "Dr Naidoo", sms)
    with pytest.raises(ValueError):
        ambulance_called(alert_id, "  ", 20, "Coordinator", sms)


# ---------------------------------------------------------------------------
# Death
# ---------------------------------------------------------------------------
def test_death_alerts_the_right_people_but_never_sms_next_of_kin(make_intake, sms):
    pid = register(make_intake, nok_phone="0825550123")
    ids = record_death(pid, "10:42", unnatural=False, staff="Dr Naidoo", sms=sms)
    types = {db.get_alert(i)["alert_type"] for i in ids}
    assert types == {"DEATH_MANAGER", "DEATH_CERTIFY", "NOK_CONTACT"}
    nok_task = next(db.get_alert(i) for i in ids if db.get_alert(i)["alert_type"] == "NOK_CONTACT")
    assert "IN PERSON or BY PHONE" in nok_task["message"]
    assert not any(mask_phone("0825550123") in m for m in sms.recent_messages(50))
    assert db.get_patient(pid)["outcome"] == "DECEASED"


def test_unnatural_death_adds_saps_task(make_intake, sms):
    pid = register(make_intake)
    ids = record_death(pid, "10:42", unnatural=True, staff="Dr Naidoo", sms=sms)
    assert "SAPS" in {db.get_alert(i)["alert_type"] for i in ids}


# ---------------------------------------------------------------------------
# Patient records
# ---------------------------------------------------------------------------
def test_records_search_by_name_id_and_queue_number(make_intake):
    register(make_intake, first_name="Lerato", surname="Mokoena", id_type="Passport", id_number="DEMO55555")
    register(make_intake, first_name="Pieter", surname="Botha")
    assert [p["surname"] for p in db.search_patients("mokoena")] == ["Mokoena"]
    assert [p["first_name"] for p in db.search_patients("DEMO555")] == ["Lerato"]
    assert len(db.search_patients("")) == 2
    assert [p["first_name"] for p in db.search_patients("2")] == ["Pieter"]  # queue number
    assert "nok_phone" in db.records_csv(db.search_patients("")).splitlines()[0]


# ---------------------------------------------------------------------------
# Referral order and the HIV testing room
# ---------------------------------------------------------------------------
from core.auth import can_see_hiv_result  # noqa: E402
from core.workflow import record_hiv_test  # noqa: E402


def at_hiv_room(make_intake, sms, stops=("HIV_TEST", "INJECTION", "PHARMACY")):
    pid = register(make_intake)
    record_vitals(pid, Vitals(**NORMAL), "Sr Dlamini", sms)
    complete_clinician(pid, "Test and treat", list(stops), "Dr Naidoo", sms)
    return pid


def test_rooms_always_follow_hiv_injection_pharmacy_order(make_intake, sms):
    pid = register(make_intake)
    record_vitals(pid, Vitals(**NORMAL), "Sr Dlamini", sms)
    nxt = complete_clinician(pid, "x", ["PHARMACY", "HIV_TEST", "INJECTION"], "Dr Naidoo", sms)
    assert nxt == "HIV_TEST"
    assert json.loads(db.get_patient(pid)["pending_stations"]) == ["INJECTION", "PHARMACY"]


def test_hiv_test_needs_consent(make_intake, sms):
    pid = at_hiv_room(make_intake, sms)
    with pytest.raises(ValueError, match="consent"):
        record_hiv_test(pid, "Negative", consented=False, staff="Counsellor Zulu", sms=sms)


def test_declined_test_needs_no_consent_and_continues(make_intake, sms):
    pid = at_hiv_room(make_intake, sms)
    assert record_hiv_test(pid, "Patient declined testing", False, "Counsellor Zulu", sms) == "INJECTION"


def test_negative_result_continues_to_next_room(make_intake, sms):
    pid = at_hiv_room(make_intake, sms)
    assert record_hiv_test(pid, "Negative", True, "Counsellor Zulu", sms) == "INJECTION"
    assert json.loads(db.get_patient(pid)["hiv_test_json"])["result"] == "Negative"


def test_positive_result_must_go_back_to_clinician(make_intake, sms):
    pid = at_hiv_room(make_intake, sms)
    positive = "Positive (confirmed with a second test)"
    with pytest.raises(ValueError, match="must go back"):
        record_hiv_test(pid, positive, True, "Counsellor Zulu", sms, back_to_clinician=False)
    assert record_hiv_test(pid, positive, True, "Counsellor Zulu", sms, back_to_clinician=True) == "CLINICIAN"
    patient = db.get_patient(pid)
    # The remaining rooms are kept, so the patient still gets the injection and medication afterwards
    assert json.loads(patient["pending_stations"]) == ["INJECTION", "PHARMACY"]


def test_hiv_result_never_in_audit_log_or_export(make_intake, sms):
    pid = at_hiv_room(make_intake, sms)
    record_hiv_test(pid, "Positive (confirmed with a second test)", True, "Counsellor Zulu", sms,
                    back_to_clinician=True)
    everything = json.dumps(db.get_audit_log())
    assert "Positive" not in everything
    assert "HIV positive" not in everything
    assert "Positive" not in db.records_csv(db.search_patients(""))


def test_only_clinical_roles_see_hiv_results():
    for role in ("Nurse", "Doctor", "HIV counsellor"):
        assert can_see_hiv_result(role)
    for role in ("Pharmacist", "Facility manager", "Admin", "Ambulance coordinator"):
        assert not can_see_hiv_result(role)
