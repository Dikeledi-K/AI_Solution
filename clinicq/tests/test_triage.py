"""Tests for the triage pipeline: rules + LLM combination, retries and safe fallback."""

import json

import pytest

from core.demo import load_sample_cases
from core.llm_client import BrokenMockClient, LLMClient, LLMError, MockLLMClient
from core.models import PatientIntake, RulesResult, Urgency
from core.triage import combine, parse_llm_output, run_triage

ALL_LEVELS = list(Urgency)


def valid_reply(urgency: str = "GREEN", **overrides) -> str:
    data = {
        "detected_language": "English",
        "english_translation": "test",
        "chief_complaint": "test",
        "symptoms": ["test"],
        "duration": None,
        "self_reported_severity": None,
        "red_flags_mentioned": [],
        "suggested_urgency": urgency,
        "reasoning": "Test reasoning.",
        "questions_for_nurse": [],
    }
    data.update(overrides)
    return json.dumps(data)


class FixedClient(LLMClient):
    """Fake LLM that always suggests the same urgency."""

    name = "fixed"

    def __init__(self, urgency: str):
        self.urgency = urgency
        self.calls = 0

    def complete(self, system_prompt, user_message):
        self.calls += 1
        return valid_reply(self.urgency)


class SequenceClient(LLMClient):
    """Fake LLM that returns a list of replies in order."""

    name = "sequence"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, system_prompt, user_message):
        self.calls += 1
        return self.replies.pop(0)


class ExplodingClient(LLMClient):
    name = "exploding"

    def complete(self, system_prompt, user_message):
        raise RuntimeError("network cable unplugged")


# ---------------------------------------------------------------------------
# Rules can NEVER be downgraded by the AI
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ai_level", ["GREEN", "YELLOW", "ORANGE"])
def test_ai_cannot_downgrade_red_rule(make_intake, ai_level):
    intake = make_intake("I have crushing chest pain")
    result = run_triage(intake, client=FixedClient(ai_level))
    assert result.rules_result.urgency == Urgency.RED
    assert result.ai_urgency.value == ai_level
    assert result.final_urgency == Urgency.RED
    assert result.decided_by == "rules"


@pytest.mark.parametrize("ai_level", ["GREEN", "YELLOW"])
def test_ai_cannot_downgrade_orange_rule(make_intake, ai_level):
    intake = make_intake("I burned my hand with boiling water")
    result = run_triage(intake, client=FixedClient(ai_level))
    assert result.final_urgency == Urgency.ORANGE
    assert result.decided_by == "rules"


def test_ai_can_raise_above_rules(make_intake):
    intake = make_intake("I burned my hand with boiling water")  # ORANGE rule
    result = run_triage(intake, client=FixedClient("RED"))
    assert result.final_urgency == Urgency.RED
    assert result.decided_by == "ai"


@pytest.mark.parametrize("rules_level", [None, Urgency.ORANGE, Urgency.RED])
@pytest.mark.parametrize("ai_level", ALL_LEVELS)
def test_final_is_always_the_higher_of_rules_and_ai(rules_level, ai_level):
    rules = RulesResult(matches=[], urgency=rules_level)
    llm = parse_llm_output(valid_reply(ai_level.value))
    final, decided_by, _ = combine(rules, llm)
    assert final.rank >= ai_level.rank
    if rules_level is not None:
        assert final.rank >= rules_level.rank
    assert decided_by in ("rules", "ai", "rules + ai (agree)")


def test_agreement_is_recorded(make_intake):
    result = run_triage(make_intake("I have chest pain"), client=FixedClient("RED"))
    assert result.decided_by == "rules + ai (agree)"


# ---------------------------------------------------------------------------
# Invalid LLM output falls back safely
# ---------------------------------------------------------------------------
def test_invalid_json_retries_once_then_falls_back_to_yellow(make_intake):
    client = SequenceClient(["not json at all", "{still not valid json"])
    result = run_triage(make_intake("I have a mild headache"), client=client)
    assert client.calls == 2  # first try + exactly one retry
    assert result.llm_output is None
    assert result.final_urgency == Urgency.YELLOW
    assert result.decided_by == "fallback"
    assert result.needs_manual_review
    assert result.llm_error


def test_retry_succeeds_on_second_attempt(make_intake):
    client = SequenceClient(["oops", valid_reply("GREEN")])
    result = run_triage(make_intake("I need my repeat medication"), client=client)
    assert result.llm_output is not None
    assert result.llm_attempts == 2
    assert result.final_urgency == Urgency.GREEN


@pytest.mark.parametrize(
    "bad_reply",
    [
        valid_reply("PURPLE"),                                # not a real urgency level
        valid_reply("GREEN", self_reported_severity=42),      # severity out of range
        json.dumps({"suggested_urgency": "GREEN"}),           # missing required fields
        "",                                                   # empty reply
    ],
)
def test_schema_violations_fall_back(make_intake, bad_reply):
    result = run_triage(make_intake("I feel unwell"), client=SequenceClient([bad_reply, bad_reply]))
    assert result.llm_output is None
    assert result.final_urgency == Urgency.YELLOW


def test_fallback_never_lowers_a_red_flag(make_intake):
    result = run_triage(make_intake("My child had a seizure"), client=BrokenMockClient())
    assert result.llm_output is None
    assert result.final_urgency == Urgency.RED
    assert result.decided_by == "rules"


def test_provider_exception_does_not_crash(make_intake):
    result = run_triage(make_intake("I feel dizzy"), client=ExplodingClient())
    assert result.final_urgency == Urgency.YELLOW
    assert "RuntimeError" in result.llm_error


def test_missing_api_key_falls_back(make_intake, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = run_triage(make_intake("I have chest pain"))
    assert result.llm_provider == "unavailable"
    assert result.final_urgency == Urgency.RED  # rules still work without AI


def test_code_fenced_json_is_accepted():
    out = parse_llm_output("```json\n" + valid_reply("ORANGE") + "\n```")
    assert out.suggested_urgency == Urgency.ORANGE


def test_lowercase_urgency_is_normalised():
    assert parse_llm_output(valid_reply("red")).suggested_urgency == Urgency.RED


# ---------------------------------------------------------------------------
# Mock provider + demo data
# ---------------------------------------------------------------------------
def test_mock_client_always_returns_valid_json():
    for case in load_sample_cases():
        intake = PatientIntake(**case["intake"])
        result = run_triage(intake, client=MockLLMClient())
        assert result.llm_output is not None, case["note"]


@pytest.mark.parametrize("case", load_sample_cases(), ids=lambda c: c["intake"]["first_name"])
def test_demo_patients_reach_expected_minimum_urgency(case):
    result = run_triage(PatientIntake(**case["intake"]), client=MockLLMClient())
    expected = Urgency(case["expected_min_urgency"])
    assert result.final_urgency.rank >= expected.rank, case["note"]


def test_mock_detects_languages():
    expectations = {"Sipho": "isiZulu", "Annatjie": "Afrikaans", "Palesa": "Sesotho", "Busi": "English"}
    for case in load_sample_cases():
        name = case["intake"]["first_name"]
        if name in expectations:
            result = run_triage(PatientIntake(**case["intake"]), client=MockLLMClient())
            assert result.llm_output.detected_language == expectations[name], name


def test_mock_understands_sepedi(make_intake):
    intake = make_intake("Ke palelwa ke go hemaga botse and ke bolawa ke mala")
    result = run_triage(intake, client=MockLLMClient())
    out = result.llm_output
    assert out.detected_language == "Sepedi"
    assert out.chief_complaint == "difficulty breathing"
    assert "abdominal pain" in out.symptoms
    assert result.final_urgency == Urgency.RED


def test_unknown_provider_raises():
    from core.llm_client import get_llm_client

    with pytest.raises(LLMError):
        get_llm_client("not-a-provider")
