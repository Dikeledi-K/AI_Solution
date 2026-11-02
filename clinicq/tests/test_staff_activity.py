"""Staff activity report: patients helped per staff member per day, with times."""

from core import auth
from core import database as db
from core.models import Vitals
from core.queue_manager import call_patient, not_here
from core.reports import staff_activity
from core.sms_service import MockSMSService
from core.triage import run_triage
from core.workflow import complete_clinician, record_vitals

NURSE, DOCTOR, CLERK = "Sr Dlamini (N1001)", "Dr Naidoo (D2001)", "Ms Nkosi (R5001)"
NORMAL = dict(bp_systolic=120, bp_diastolic=80, temperature=36.8, pulse=78)


def setup_staff():
    auth.add_staff("N1001", "Sr Dlamini", "Nurse", "4826")
    auth.add_staff("D2001", "Dr Naidoo", "Doctor", "7391")
    auth.add_staff("R5001", "Ms Nkosi", "Registration clerk", "3958")


def register(make_intake, name):
    intake = make_intake("I need my repeat medication", first_name=name)
    return db.add_patient(intake, run_triage(intake), registered_by=CLERK)


def test_counts_patients_per_staff_member_with_roles_and_times(make_intake, tmp_path):
    setup_staff()
    sms = MockSMSService(tmp_path / "sms.log")
    a, b = register(make_intake, "Ann"), register(make_intake, "Ben")
    for pid in (a, b):
        call_patient(pid, sms, room="Vitals Room 1", staff=NURSE)
        record_vitals(pid, Vitals(**NORMAL), NURSE, sms)
    call_patient(a, sms, room="Consultation Room 1", staff=DOCTOR)
    complete_clinician(a, "Rest and fluids", [], DOCTOR, sms)

    rows = {r["staff_id"]: r for r in staff_activity()}
    assert rows["N1001"]["patients"] == 2 and rows["N1001"]["role"] == "Nurse"
    assert rows["D2001"]["patients"] == 1 and rows["R5001"]["patients"] == 2
    nurse_visit = rows["N1001"]["visits"][0]
    assert "Called" in nurse_visit["did"] and "Vitals" in nurse_visit["did"]
    assert nurse_visit["from"] <= nurse_visit["to"]
    doctor_visit = rows["D2001"]["visits"][0]
    assert "Sent home" in doctor_visit["did"]
    assert [r["staff_id"] for r in staff_activity()][0] in ("N1001", "R5001")  # busiest first


def test_registration_alone_has_no_duration(make_intake):
    setup_staff()
    register(make_intake, "Ann")
    clerk = staff_activity()[0]
    assert clerk["patients"] == 1 and clerk["visits"][0]["minutes"] is None and clerk["avg_minutes"] is None


def test_sign_in_and_out_times_are_shown(make_intake, tmp_path):
    setup_staff()
    auth.authenticate("N1001", "4826")
    pid = register(make_intake, "Ann")
    call_patient(pid, MockSMSService(tmp_path / "sms.log"), room="Vitals Room 1", staff=NURSE)
    db.add_audit("STAFF_LOGIN", nurse_action=f"{NURSE} signed out", staff=NURSE)
    nurse = next(r for r in staff_activity() if r["staff_id"] == "N1001")
    assert nurse["signed_in"] and nurse["signed_out"]


def test_not_here_is_recorded_for_the_staff_member(make_intake, tmp_path):
    setup_staff()
    pid = register(make_intake, "Ann")
    call_patient(pid, MockSMSService(tmp_path / "sms.log"), room="Vitals Room 1", staff=NURSE)
    not_here(pid, NURSE)
    nurse = next(r for r in staff_activity() if r["staff_id"] == "N1001")
    assert nurse["visits"][0]["did"] == "Called, Not here"


def test_other_days_and_non_patient_events_are_not_counted(make_intake):
    setup_staff()
    db.add_audit("RECORD_VIEW", patient_id=register(make_intake, "Ann"), nurse_action=f"viewed by {NURSE}",
                 staff=NURSE)
    assert all(r["staff_id"] != "N1001" for r in staff_activity())  # viewing a record is not helping a patient
    assert staff_activity(day=__import__("datetime").date(2000, 1, 1)) == []


def test_staff_field_keeps_the_audit_chain_valid(make_intake):
    setup_staff()
    register(make_intake, "Ann")
    assert db.verify_audit_chain()["ok"]
