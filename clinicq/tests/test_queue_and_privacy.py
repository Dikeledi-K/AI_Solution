"""Tests for the queue, audit log, SMS triggers and privacy helpers."""

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from core import database as db
from core.llm_client import MockLLMClient
from core.models import Urgency
from core.privacy import mask_phone
from core.queue_manager import (AlreadyCalledError, RoomBusyError, call_patient,
                                get_waiting_queue,
                                is_overdue, log_new_overdue, moved_up_for_waiting,
                                not_here, notify_near_front, queue_stats)
from core.sms_service import MockSMSService
from core.workflow import complete_station
from core.triage import run_triage


def add(make_intake, text, arrival, **kw):
    intake = make_intake(text, **kw)
    return db.add_patient(intake, run_triage(intake, client=MockLLMClient()), arrival_time=arrival)


def test_queue_sorted_by_urgency_then_arrival(make_intake):
    add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")      # GREEN, first
    add(make_intake, "I have a mild headache and cough", "2026-01-01T08:05:00")  # GREEN
    add(make_intake, "I have chest pain", "2026-01-01T08:30:00")                 # RED, last in
    queue = get_waiting_queue()
    assert queue[0]["final_urgency"] == "RED"
    assert [p["arrival_time"] for p in queue[1:]] == ["2026-01-01T08:00:00", "2026-01-01T08:05:00"]


def test_nurse_override_changes_order_and_is_audited(make_intake):
    green_id = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    add(make_intake, "I burned my hand with boiling water", "2026-01-01T08:10:00")  # ORANGE
    db.set_nurse_urgency(green_id, Urgency.RED, "Looks very pale and sweaty", override=True)
    assert get_waiting_queue()[0]["id"] == green_id
    overrides = db.get_audit_log("NURSE_OVERRIDE")
    assert len(overrides) == 1 and "pale" in overrides[0]["details"]


def test_every_triage_is_audited(make_intake):
    add(make_intake, "I have chest pain", "2026-01-01T08:00:00")
    entries = db.get_audit_log("TRIAGE")
    assert len(entries) == 1
    assert entries[0]["final_urgency"] == "RED"
    assert "chest_pain" in entries[0]["rules_result"]
    assert "queue_number" in db.audit_log_csv().splitlines()[0]


def test_near_front_sms_sent_once(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    for i in range(5):
        add(make_intake, "I need my repeat medication", f"2026-01-01T08:0{i}:00", phone="0821234567")
    notify_near_front(sms)
    notify_near_front(sms)  # second call must not resend
    assert len(sms.recent_messages(50)) == 3  # positions 1, 2 and 3 (0-2 people ahead)


def test_call_sends_sms_and_updates_status(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    pid = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00", phone="0821234567")
    call_patient(pid, sms)
    assert db.get_patient(pid)["status"] == "called"
    assert any("please come to Room 1" in m for m in sms.recent_messages())
    # The SMS log never contains the full phone number.
    assert all("0821234567" not in m for m in sms.recent_messages())


def test_stats(make_intake):
    add(make_intake, "I have chest pain", "2026-01-01T08:00:00")
    add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    stats = queue_stats()
    assert stats["waiting_total"] == 2
    assert stats["per_colour"]["RED"] == 1 and stats["per_colour"]["GREEN"] == 1
    assert stats["awaiting_review"] == 2


# ---------------------------------------------------------------------------
# Waiting-time fairness (target times per colour)
# ---------------------------------------------------------------------------
def names(queue):
    return [p["first_name"] for p in queue]


def test_overdue_green_moves_ahead_of_later_yellow(make_intake):
    add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00", first_name="Green")
    add(make_intake, "I feel dizzy", "2026-01-01T11:30:00", first_name="Yellow")
    now = datetime(2026, 1, 1, 12, 30)  # Green has waited 4.5 h (> 4 h target)
    queue = get_waiting_queue(now)
    assert names(queue) == ["Green", "Yellow"]
    assert queue[0]["final_urgency"] == "GREEN"  # colour label is NOT changed
    assert moved_up_for_waiting(queue[0], now)


def test_green_within_target_stays_behind_yellow(make_intake):
    add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00", first_name="Green")
    add(make_intake, "I feel dizzy", "2026-01-01T09:30:00", first_name="Yellow")
    now = datetime(2026, 1, 1, 10, 0)  # Green has waited only 2 h
    assert names(get_waiting_queue(now)) == ["Yellow", "Green"]


def test_overdue_patients_never_jump_orange_or_red(make_intake):
    add(make_intake, "I need my repeat medication", "2026-01-01T06:00:00", first_name="Green")
    add(make_intake, "I feel dizzy", "2026-01-01T07:00:00", first_name="Yellow")
    add(make_intake, "I burned my hand with boiling water", "2026-01-01T12:55:00", first_name="Orange")
    add(make_intake, "I have chest pain", "2026-01-01T12:59:00", first_name="Red")
    now = datetime(2026, 1, 1, 13, 0)  # Green and Yellow are both hours overdue
    assert names(get_waiting_queue(now)) == ["Red", "Orange", "Green", "Yellow"]


@pytest.mark.parametrize(
    "text, waited, expected",
    [
        ("I have chest pain", 0, False),                     # RED: target immediately
        ("I have chest pain", 1, True),
        ("I burned my hand with boiling water", 10, False),  # ORANGE: 10 min
        ("I burned my hand with boiling water", 11, True),
        ("I feel dizzy", 60, False),                         # YELLOW: 1 h
        ("I feel dizzy", 61, True),
        ("I need my repeat medication", 240, False),         # GREEN: 4 h
        ("I need my repeat medication", 241, True),
    ],
)
def test_overdue_uses_colour_target(make_intake, text, waited, expected):
    pid = add(make_intake, text, "2026-01-01T08:00:00")
    now = datetime(2026, 1, 1, 8, 0) + timedelta(minutes=waited)
    assert is_overdue(db.get_patient(pid), now) is expected


def test_nurse_downgrade_uses_new_colour_target(make_intake):
    pid = add(make_intake, "I feel dizzy", "2026-01-01T08:00:00")  # YELLOW, 1 h target
    now = datetime(2026, 1, 1, 9, 30)
    assert is_overdue(db.get_patient(pid), now)
    db.set_nurse_urgency(pid, Urgency.GREEN, "Checked: stable, routine", override=True)
    assert not is_overdue(db.get_patient(pid), now)  # GREEN target is 4 h


def test_overdue_is_logged_once(make_intake):
    add(make_intake, "I feel dizzy", "2026-01-01T08:00:00")  # YELLOW, 1 h target
    assert log_new_overdue(datetime(2026, 1, 1, 8, 30)) == []  # not overdue yet
    assert log_new_overdue(datetime(2026, 1, 1, 9, 30)) == [1]
    assert log_new_overdue(datetime(2026, 1, 1, 9, 45)) == []  # no duplicate entry
    entries = db.get_audit_log("OVERDUE")
    assert len(entries) == 1
    assert "Waited 90 min" in entries[0]["details"]


def test_overdue_logged_again_after_colour_change(make_intake):
    pid = add(make_intake, "I feel dizzy", "2026-01-01T08:00:00")
    log_new_overdue(datetime(2026, 1, 1, 9, 30))
    db.set_nurse_urgency(pid, Urgency.ORANGE, "Looks unwell", override=True)
    assert log_new_overdue(datetime(2026, 1, 1, 9, 31)) == [1]  # new 10-min target also missed
    assert len(db.get_audit_log("OVERDUE")) == 2


def test_staff_entered_arrival_is_recorded(make_intake):
    intake = make_intake("I need my repeat medication")
    pid = db.add_patient(intake, run_triage(intake, client=MockLLMClient()),
                         arrival_time="2026-01-01T06:30:00", arrival_entered_by_staff=True)
    patient = db.get_patient(pid)
    assert patient["arrival_time"] == "2026-01-01T06:30:00"
    assert patient["arrival_entered_by_staff"] == 1
    assert "entered by staff" in db.get_audit_log("TRIAGE")[0]["details"]


def test_old_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY, first_name TEXT)")  # old schema
    conn.close()
    monkeypatch.setenv("CLINICQ_DB_PATH", str(path))
    db.init_db()
    with db.get_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(patients)")}
    assert {"overdue_logged", "arrival_entered_by_staff"} <= columns


# ---------------------------------------------------------------------------
# Privacy: one called patient per consulting room
# ---------------------------------------------------------------------------
def test_cannot_call_two_patients_to_the_same_room(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    first = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    second = add(make_intake, "I need my repeat medication", "2026-01-01T08:05:00")
    call_patient(first, sms)
    with pytest.raises(RoomBusyError, match="#1"):
        call_patient(second, sms)
    assert db.get_patient(second)["status"] == "waiting"  # nothing changed


def test_room_is_free_again_after_seen(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    first = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    second = add(make_intake, "I need my repeat medication", "2026-01-01T08:05:00")
    call_patient(first, sms)
    complete_station(first, "Sr Test", sms)
    call_patient(second, sms)
    assert db.patient_in_room("Room 1")["id"] == second


def test_not_here_frees_room_and_keeps_place_in_queue(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    first = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    add(make_intake, "I need my repeat medication", "2026-01-01T08:05:00")
    call_patient(first, sms)
    not_here(first)
    assert db.patient_in_room("Room 1") is None
    patient = db.get_patient(first)
    assert patient["status"] == "waiting" and patient["called_time"] is None
    assert get_waiting_queue()[0]["id"] == first  # original arrival time kept
    assert "Not here" in db.get_audit_log("STATUS")[0]["nurse_action"]


def test_different_rooms_can_each_call_one_patient(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    a = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    b = add(make_intake, "I need my repeat medication", "2026-01-01T08:05:00", phone="0821234567")
    c = add(make_intake, "I need my repeat medication", "2026-01-01T08:10:00")
    call_patient(a, sms, room="Consultation Room 1")
    call_patient(b, sms, room="Consultation Room 2")
    with pytest.raises(RoomBusyError):
        call_patient(c, sms, room="Consultation Room 2")
    assert any("Consultation Room 2" in m for m in sms.recent_messages())


def test_room_names_are_kept_exactly(make_intake, tmp_path):
    """Regression: names with capitals or brackets must not be changed when calling."""
    sms = MockSMSService(tmp_path / "out.log")
    a = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    b = add(make_intake, "I need my repeat medication", "2026-01-01T08:05:00")
    call_patient(a, sms, room="Emergency Room 1 (Resus)")
    call_patient(b, sms, room="HIV Counselling Room 1")
    assert set(db.called_by_room()) == {"Emergency Room 1 (Resus)", "HIV Counselling Room 1"}
    assert db.patient_in_room("HIV Counselling Room 1")["id"] == b


def test_two_nurses_calling_the_same_patient(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "out.log")
    pid = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    call_patient(pid, sms, room="Room 1")
    with pytest.raises(AlreadyCalledError, match="just called to Room 1 by another staff member"):
        call_patient(pid, sms, room="Room 2")  # nurse in room 2 clicked a moment later
    assert db.patient_in_room("Room 2") is None


def test_old_numeric_rooms_are_renamed(make_intake):
    pid = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00")
    with db.get_connection() as conn:
        conn.execute("UPDATE patients SET status = 'called', room = '3' WHERE id = ?", (pid,))
    db.init_db()
    assert db.get_patient(pid)["room"] == "Room 3"


def test_mask_phone():
    assert mask_phone("0821234567") == "082***4567"
    assert mask_phone("+27 82 123 4567") == "082***4567"
    assert mask_phone(None) == "—"


def test_id_numbers_are_removed_from_text(make_intake):
    intake = make_intake("My ID is 9001015800087 and I have a cough")
    assert "9001015800087" not in intake.symptoms_text


def test_first_name_rejects_numbers(make_intake):
    with pytest.raises(ValidationError):
        make_intake("cough", first_name="9001015800087")


def test_male_cannot_be_pregnant(make_intake):
    with pytest.raises(ValidationError):
        make_intake("cough", sex="Male", pregnant="Yes")


# ---------------------------------------------------------------------------
# Audit log: patient name and filled-in values
# ---------------------------------------------------------------------------
def test_audit_entries_name_the_patient_and_their_colour(make_intake, tmp_path):
    pid = add(make_intake, "I have chest pain", "2026-01-01T08:00:00", first_name="Thabo", surname="Mahlangu")
    triage = db.get_audit_log("TRIAGE")[0]
    assert triage["patient_name"] == "Thabo Mahlangu"
    assert triage["nurse_action"] == "Registered and triaged"
    db.set_nurse_urgency(pid, Urgency.ORANGE, "Stable on review", override=True, staff="Sr Dlamini (N1001)")
    db.add_audit("STATUS", patient_id=pid, nurse_action="Called to Vitals Room 1")
    status = db.get_audit_log("STATUS")[0]
    assert status["patient_name"] == "Thabo Mahlangu"
    assert status["queue_number"] == 1
    assert status["final_urgency"] == "ORANGE"  # the colour at that moment


def test_staff_events_have_no_patient():
    db.add_audit("STAFF_LOGIN", nurse_action="Sr Dlamini (N1001, Nurse) signed in")
    entry = db.get_audit_log("STAFF_LOGIN")[0]
    assert entry["patient_name"] is None and entry["queue_number"] is None


def test_old_audit_entries_get_the_patient_name_but_not_a_guessed_colour(make_intake):
    pid = add(make_intake, "I need my repeat medication", "2026-01-01T08:00:00", first_name="Busi",
              surname="Mokoena")
    with db.get_connection() as conn:  # an entry written before patient names were stored
        conn.execute("INSERT INTO audit_log (timestamp, event_type, patient_id) VALUES (?, 'SMS', ?)",
                     ("2026-01-01T08:01:00", pid))
    db.init_db()  # runs the migration
    old = [e for e in db.get_audit_log("SMS") if e["timestamp"] == "2026-01-01T08:01:00"][0]
    assert old["patient_name"] == "Busi Mokoena"
    assert old["nurse_action"] == "SMS sent (simulated)"
    assert old["final_urgency"] is None  # never invented after the fact


def test_audit_csv_includes_patient_name(make_intake):
    add(make_intake, "I have chest pain", "2026-01-01T08:00:00", first_name="Thabo", surname="Mahlangu")
    lines = db.audit_log_csv().splitlines()
    assert "patient_name" in lines[0] and "Thabo Mahlangu" in "\n".join(lines[1:])
