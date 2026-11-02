"""The facility manager's daily report: counts, waiting times and nurse changes per language."""

from core import database as db
from core.models import Urgency
from core.queue_manager import call_patient
from core.reports import daily_report
from core.sms_service import MockSMSService
from core.triage import run_triage


def register(make_intake, text, **fields):
    intake = make_intake(text, **fields)
    return db.add_patient(intake, run_triage(intake))


def test_report_counts_colours_waits_and_outcomes(make_intake, tmp_path):
    sms = MockSMSService(tmp_path / "sms.log")
    red = register(make_intake, "I have chest pain")
    green = register(make_intake, "I need my repeat medication")
    call_patient(red, sms, room="Vitals Room 1")
    db.finish_visit(green, "DISCHARGED", staff="Sr Dlamini")
    r = daily_report()
    assert r["registered"] == 2 and r["finished"] == 1 and r["still_in_clinic"] == 1
    assert r["by_colour"]["RED"] == 1 and r["by_colour"]["GREEN"] == 1
    assert r["wait_to_vitals"]["RED"]["called"] == 1
    assert r["wait_to_vitals"]["RED"]["within_target_pct"] == 100  # called within the same minute
    assert r["outcomes"]["DISCHARGED"] == 1


def test_report_shows_nurse_changes_per_language(make_intake):
    zulu = register(make_intake, "Ngiyakhwehlela kakhulu")      # mock AI: isiZulu, not urgent
    english = register(make_intake, "I need my repeat medication")
    db.set_nurse_urgency(zulu, Urgency.ORANGE, "Breathing fast", override=True, staff="Sr Dlamini")
    db.set_nurse_urgency(english, Urgency(db.get_patient(english)["final_urgency"]), None, override=False)
    langs = daily_report()["languages"]
    raised = {lang: v["raised"] for lang, v in langs.items()}
    assert sum(raised.values()) == 1
    assert all(v["reviewed"] == 1 for v in langs.values())


def test_report_never_contains_names(make_intake):
    register(make_intake, "I have chest pain", first_name="Thandeka", surname="Zwane")
    assert "Thandeka" not in str(daily_report()) and "Zwane" not in str(daily_report())
