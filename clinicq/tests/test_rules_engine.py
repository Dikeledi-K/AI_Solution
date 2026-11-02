"""Tests for the red-flag rules engine."""

import json

import pytest

from core.models import Urgency
from core.rules_engine import check_red_flags, check_red_flags_for, load_rules, normalise_text


@pytest.mark.parametrize(
    "text, language",
    [
        ("I have bad chest pain since this morning", "en"),
        ("Isifuba sibuhlungu kakhulu", "zu"),
        ("Ke na le bohloko sefubeng", "st"),
        ("Ek het borspyn", "af"),
        ("Ke opša ke sehuba kudu", "nso"),
    ],
)
def test_chest_pain_is_red_in_all_five_languages(make_intake, text, language):
    result = check_red_flags(make_intake(text))
    assert result.urgency == Urgency.RED
    assert any(m.rule_id == "chest_pain" and m.language == language for m in result.matches)


@pytest.mark.parametrize(
    "text",
    [
        "Ke palelwa ke go hemaga botse and ke bolawa ke mala",  # real user entry, with typos
        "Ke palelwa ke go hema gabotse",
        "Ke hema ka bothata kudu",
    ],
)
def test_sepedi_difficulty_breathing_is_red(make_intake, text):
    result = check_red_flags(make_intake(text))
    assert result.urgency == Urgency.RED
    assert any(m.rule_id == "breathing" and m.language == "nso" for m in result.matches)


def test_sepedi_child_not_drinking_is_red(make_intake):
    result = check_red_flags(make_intake("Ngwana ga a nwe e bile o robala kudu", age=1))
    assert result.urgency == Urgency.RED


def test_matching_ignores_case_and_punctuation(make_intake):
    result = check_red_flags(make_intake("CHEST-PAIN!!! and I CAN'T BREATHE"))
    assert result.urgency == Urgency.RED
    assert {m.rule_id for m in result.matches} == {"chest_pain", "breathing"}


def test_routine_text_has_no_red_flags(make_intake):
    result = check_red_flags(make_intake("I came for my repeat medication, I feel fine."))
    assert result.urgency is None
    assert result.matches == []


def test_afrikaans_boiling_water_burn_is_orange(make_intake):
    result = check_red_flags(make_intake("Ek het my hand met 'n kookwater gebrand"))
    assert result.urgency == Urgency.ORANGE


def test_pregnancy_bleeding_only_red_when_pregnant(make_intake):
    text = "Since last night I am bleeding a little"
    assert check_red_flags(make_intake(text, pregnant="Yes")).urgency == Urgency.RED
    assert check_red_flags(make_intake(text, pregnant="No")).urgency is None


def test_pregnancy_mentioned_in_text_counts(make_intake):
    # Form says N/A, but the patient wrote that she is pregnant (isiZulu: ngikhulelwe).
    result = check_red_flags(make_intake("Ngikhulelwe futhi ngiyopha", pregnant="N/A"))
    assert result.urgency == Urgency.RED
    assert any(m.rule_id == "pregnancy_danger" for m in result.matches)


def test_child_danger_rule_only_applies_to_children(make_intake):
    text = "He is not drinking and very drowsy"
    assert check_red_flags(make_intake(text, age=2, sex="Male")).urgency == Urgency.RED
    adult = check_red_flags(make_intake(text, age=30, sex="Male"))
    assert all(m.rule_id != "child_danger" for m in adult.matches)


def test_highest_matching_rule_wins(make_intake):
    # head injury (ORANGE) + unconscious (RED) -> RED
    result = check_red_flags(make_intake("He hit my head and then he was unconscious"))
    assert result.urgency == Urgency.RED


def test_rules_file_only_contains_orange_or_red():
    rules = load_rules()
    assert rules["rules"], "rules file should not be empty"
    for rule in rules["rules"]:
        assert rule["urgency"] in ("RED", "ORANGE"), rule["id"]
        assert set(rule["phrases"]) == {"en", "zu", "st", "nso", "af"}, f"{rule['id']} missing a language"


def test_loader_rejects_rules_that_would_lower_urgency(tmp_path):
    bad = {"rules": [{"id": "x", "name": "x", "urgency": "GREEN", "phrases": {"en": ["x"]}}]}
    path = tmp_path / "bad_rules.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="ORANGE or RED"):
        load_rules(path)


def test_normalise_text():
    assert normalise_text("  Can't   BREATHE!! ") == "cant breathe"


# ---------------------------------------------------------------------------
# Negation: clear English negations do not raise the colour; anything unclear still does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "I have no chest pain, just a cough",
    "She denies any chest pain",
    "I don't have chest pain",
    "Without any real chest pain",
])
def test_clearly_negated_danger_sign_does_not_raise_but_is_shown(text):
    result = check_red_flags_for(text, 35, False)
    assert result.urgency is None
    assert [m.rule_id for m in result.negated] == ["chest_pain"]


@pytest.mark.parametrize("text", [
    "No. Chest pain since this morning",           # the "no" answers something else
    "No fever but chest pain",                     # a new clause
    "No fever and chest pain",
    "No fever or chest pain",                      # unclear: safe side
    "I am not well, chest pain",
    "I do not know why I have chest pain",         # negation too far away
    "Not only chest pain but also a cough",
    "No chest pain earlier. Chest pain now",       # mentioned again without negation
])
def test_unclear_or_partial_negation_still_fires(text):
    assert check_red_flags_for(text, 35, False).urgency == Urgency.RED


def test_negation_never_switches_off_danger_signs_that_are_negative_phrases():
    # "not drinking" IS the danger sign for a small child
    assert check_red_flags_for("My baby is not drinking", 1, False).urgency == Urgency.RED
    assert check_red_flags_for("He cannot breathe", 40, False).urgency == Urgency.RED


def test_negation_is_english_only_until_reviewed_by_native_speakers():
    # isiZulu chest pain is still caught even if English "no chest pain" is also written
    result = check_red_flags_for("Isifuba sibuhlungu, no chest pain", 35, False)
    assert result.urgency == Urgency.RED
