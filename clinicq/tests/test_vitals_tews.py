"""Fuller vital signs: SpO2, glucose, AVPU, children's weight and the adult SATS TEWS score."""

import json

import pytest

from core import database as db
from core.models import Urgency, Vitals
from core.sms_service import MockSMSService
from core.triage import run_triage
from core.vitals import check_vitals, tews_score, vitals_text
from core.workflow import record_vitals

CALM_ADULT = dict(bp_systolic=120, bp_diastolic=80, temperature=36.8, pulse=78, resp_rate=14,
                  mobility="Walking", avpu="Alert", trauma=False)


def urgency_of(flags):
    return max((f.urgency for f in flags), key=lambda u: u.rank, default=None)


def test_calm_adult_scores_tews_zero_and_raises_nothing():
    v = Vitals(**CALM_ADULT)
    assert tews_score(v, 35)["score"] == 0
    assert check_vitals(v, pregnant=False, age=35) == []


@pytest.mark.parametrize("changes, score, colour", [
    ({"resp_rate": 22, "pulse": 112}, 4, Urgency.YELLOW),                         # 2 + 2
    ({"resp_rate": 22, "pulse": 112, "mobility": "With help"}, 5, Urgency.ORANGE),  # 2 + 2 + 1
    ({"resp_rate": 31, "pulse": 112, "mobility": "Stretcher / immobile"}, 7, Urgency.RED),  # 3 + 2 + 2
])
def test_adult_tews_score_sets_the_colour(changes, score, colour):
    v = Vitals(**(CALM_ADULT | changes))
    assert tews_score(v, 35)["score"] == score
    assert any(f.name == f"TEWS {score}" and f.urgency == colour for f in check_vitals(v, False, 35))


def test_tews_needs_all_seven_parts_and_is_adult_only():
    assert tews_score(Vitals(**(CALM_ADULT | {"mobility": None})), 35) is None
    assert tews_score(Vitals(**CALM_ADULT), 12) is None  # children need the paediatric chart


@pytest.mark.parametrize("changes, colour", [
    ({"spo2": 89}, Urgency.ORANGE),
    ({"glucose": 2.4}, Urgency.ORANGE),
    ({"glucose": 15.2}, Urgency.YELLOW),
    ({"avpu": "Unresponsive"}, Urgency.RED),
])
def test_single_danger_readings(changes, colour):
    v = Vitals(**(dict(bp_systolic=120, bp_diastolic=80, temperature=36.8) | changes))
    assert urgency_of(check_vitals(v, pregnant=False)) == colour


def test_child_vitals_need_a_weight(make_intake, tmp_path):
    intake = make_intake("Fever and cough", age=4)
    pid = db.add_patient(intake, run_triage(intake))
    sms = MockSMSService(tmp_path / "sms.log")
    base = dict(bp_systolic=95, bp_diastolic=60, temperature=37.9)
    with pytest.raises(ValueError, match="weight"):
        record_vitals(pid, Vitals(**base), "Sr Dlamini", sms)
    record_vitals(pid, Vitals(**base, weight_kg=16.5), "Sr Dlamini", sms)
    assert json.loads(db.get_patient(pid)["vitals_json"])["weight_kg"] == 16.5


def test_tews_is_saved_and_raises_the_colour(make_intake, tmp_path):
    intake = make_intake("I need my repeat medication", age=60)
    pid = db.add_patient(intake, run_triage(intake))
    sms = MockSMSService(tmp_path / "sms.log")
    sick = Vitals(**(CALM_ADULT | {"resp_rate": 31, "pulse": 112, "mobility": "Stretcher / immobile"}))
    result = record_vitals(pid, sick, "Sr Dlamini", sms)
    assert result["final"] == Urgency.RED
    stored = json.loads(db.get_patient(pid)["vitals_json"])
    assert stored["tews"] == 7 and "TEWS 7" in vitals_text(stored)
