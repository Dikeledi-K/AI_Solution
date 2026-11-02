"""Registration at reception: folder numbers, returning patients, daily queue numbers
and protection against registering the same person twice."""

import pytest

from core import database as db
from core.triage import run_triage

SA_ID = "8501015800088"  # valid checksum, born 1 Jan 1985


def register(make_intake, text="I need my repeat medication", folder=None, by="Ms Nkosi (R5001)", **fields):
    intake = make_intake(text, **fields)
    return db.add_patient(intake, run_triage(intake), folder_number=folder, registered_by=by)


def finish(pid):
    db.finish_visit(pid, "DISCHARGED", staff="Sr Dlamini")


def test_new_patient_gets_a_folder_number_and_the_clerk_is_recorded(make_intake):
    p = db.get_patient(register(make_intake))
    assert p["folder_number"] == "F000001"
    assert p["registered_by"] == "Ms Nkosi (R5001)"
    triage = db.get_audit_log("TRIAGE")[0]
    assert triage["nurse_action"] == "Registered by Ms Nkosi (R5001)" and "folder=F000001" in triage["details"]


def test_each_new_person_gets_the_next_folder_number(make_intake):
    a = db.get_patient(register(make_intake, first_name="Ann"))
    b = db.get_patient(register(make_intake, first_name="Ben"))
    assert (a["folder_number"], b["folder_number"]) == ("F000001", "F000002")


def test_returning_patient_keeps_their_folder(make_intake):
    first = register(make_intake, first_name="Thandi", surname="Mokoena")
    folder = db.get_patient(first)["folder_number"]
    finish(first)
    second = register(make_intake, folder=folder, first_name="Thandi", surname="Mokoena")
    assert db.get_patient(second)["folder_number"] == folder
    assert [v["id"] for v in db.visits_for_folder(folder)] == [second, first]


def test_an_id_number_with_a_folder_is_linked_automatically(make_intake):
    first = register(make_intake, id_type="SA ID", id_number=SA_ID, age=db_age())
    finish(first)
    second = register(make_intake, id_type="SA ID", id_number=SA_ID, age=db_age())  # clerk did not search
    assert db.get_patient(second)["folder_number"] == db.get_patient(first)["folder_number"]


def db_age() -> int:
    from datetime import date
    from core.privacy import age_on
    return age_on(date(1985, 1, 1))


def test_a_person_already_in_the_clinic_cannot_be_registered_twice(make_intake):
    pid = register(make_intake, id_type="SA ID", id_number=SA_ID, age=db_age())
    folder = db.get_patient(pid)["folder_number"]
    with pytest.raises(db.AlreadyInClinic, match="#1"):
        register(make_intake, folder=folder)
    with pytest.raises(db.AlreadyInClinic):  # also when found through the ID number
        register(make_intake, id_type="SA ID", id_number=SA_ID, age=db_age())


def test_search_finds_returning_patients_by_folder_id_or_name(make_intake):
    pid = register(make_intake, first_name="Thandi", surname="Mokoena", id_type="SA ID", id_number=SA_ID,
                   age=db_age())
    finish(pid)
    register(make_intake, first_name="Sipho", surname="Dube")
    for query in ("F000001", "f000001", SA_ID, "Mokoena", "thandi mok"):
        found = db.find_returning_patients(query)
        assert [r["folder_number"] for r in found] == ["F000001"], query
    assert db.find_returning_patients("x") == []  # too short to search


def test_search_returns_one_row_per_person_the_latest_visit(make_intake):
    first = register(make_intake, first_name="Thandi", surname="Mokoena", phone="0821111111")
    folder = db.get_patient(first)["folder_number"]
    finish(first)
    second = register(make_intake, folder=folder, first_name="Thandi", surname="Mokoena", phone="0822222222")
    found = db.find_returning_patients("Mokoena")
    assert len(found) == 1 and found[0]["id"] == second and found[0]["phone"] == "0822222222"


def test_queue_numbers_start_again_each_day(make_intake):
    a = register(make_intake, first_name="Ann")
    with db.get_connection() as conn:  # pretend Ann came yesterday
        conn.execute("UPDATE patients SET visit_date = '2000-01-01' WHERE id = ?", (a,))
    b = register(make_intake, first_name="Ben")
    c = register(make_intake, first_name="Cal")
    assert db.get_patient(b)["queue_number"] == 1 and db.get_patient(c)["queue_number"] == 2


def test_folder_numbers_are_never_reused_after_a_demo_reset(make_intake):
    register(make_intake)
    db.clear_all()
    assert db.get_patient(register(make_intake))["folder_number"] == "F000002"


def test_records_search_finds_a_folder(make_intake):
    register(make_intake, first_name="Thandi")
    assert [r["first_name"] for r in db.search_patients("F000001")] == ["Thandi"]
