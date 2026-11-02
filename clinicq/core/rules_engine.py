"""Red-flag rules engine – runs FIRST, before any AI.

Why rules before AI?
    An LLM can mistranslate, misunderstand or simply be unavailable. Danger signs
    such as chest pain or a child who is not drinking must ALWAYS be caught, so we
    check them with simple, transparent keyword rules that clinic staff can read
    and edit in data/red_flags.json.

Rules can only RAISE urgency. They return ORANGE or RED, or None (no opinion).

Negation (English only, deliberately cautious):
    "No chest pain, just a cough" should not make a patient RED. A danger sign is
    treated as ABSENT only when, in the same clause, it directly follows a clear
    negation such as "no", "not", "without", "denies" or "don't have" (optionally
    with a word like "any" or "real" in between). Anything less clear still fires,
    because over-triage is the safer mistake. A negated danger sign never raises the
    colour but is listed for the nurse to check. Other languages are not negated
    until native-speaking nurses have reviewed the wording.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from core.models import PatientIntake, RuleMatch, RulesResult, Urgency, max_urgency

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "red_flags.json"

# Rules are only allowed to push urgency UP to one of these levels.
ALLOWED_RULE_LEVELS = {Urgency.RED, Urgency.ORANGE}


def normalise_text(text: str) -> str:
    """Lower-case, turn punctuation into spaces and collapse whitespace.

    "Chest-PAIN!!" -> "chest pain". Apostrophes are removed so that
    "can't" and "cant" both become "cant".
    """
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=8)
def load_rules(path: str | Path = DEFAULT_RULES_PATH) -> dict:
    """Load and sanity-check the rules file. Cached so it is only read once."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    for rule in data["rules"]:
        level = Urgency(rule["urgency"])
        if level not in ALLOWED_RULE_LEVELS:
            raise ValueError(
                f"Rule '{rule['id']}' has urgency {level.value}. "
                "Red-flag rules may only be ORANGE or RED."
            )
    return data


def _find_phrase(phrase: str, normalised_text: str) -> bool:
    """True if the phrase appears at the START of a word in the text.

    We only anchor the start, so 'bleed' matches 'bleeding' and the isiZulu
    stem 'ngiyopha' matches 'ngiyopha kakhulu'.
    """
    pattern = r"(?<!\w)" + re.escape(normalise_text(phrase))
    return re.search(pattern, normalised_text) is not None


# --- Negation ---------------------------------------------------------------------
NEGATION_LANGUAGES = {"en"}
# Clauses are split at punctuation and at words that start a new thought, so
# "no fever but chest pain" and "no fever, chest pain" still catch the chest pain.
_CLAUSE_SPLIT = re.compile(r"[.,;:!?()\n]+|\b(?:but|however|although|though|and|then|now|except)\b", re.I)
_NEGATION_CUES = (r"no|not|without|denies|denied|never|dont have|do not have|doesnt have|does not have|"
                  r"didnt have|did not have|havent had|have not had|hasnt had|has not had|free of|negative for")
# Only these small words may stand between the negation and the danger sign.
_FILLER = r"(?:any|real|more|much|bad|sign|signs|of|the|a|an|history|complaints?)"
_NEGATED_BEFORE = re.compile(rf"(?:^|\s)(?:{_NEGATION_CUES})(?:\s+{_FILLER}){{0,2}}\s*$")


def _clauses(text: str) -> list[str]:
    """Split the ORIGINAL text into clauses, then normalise each one."""
    text = text.replace("'", "").replace("’", "")  # "don't" -> "dont", as normalise_text does
    return [c for c in (normalise_text(part) for part in _CLAUSE_SPLIT.split(text)) if c]


def _occurrences(phrase: str, clause: str) -> list[bool]:
    """For each place the phrase appears in the clause: True if it is negated there."""
    pattern = re.compile(r"(?<!\w)" + re.escape(normalise_text(phrase)))
    return [bool(_NEGATED_BEFORE.search(clause[: m.start()])) for m in pattern.finditer(clause)]


def _match_rule(phrases_by_lang: dict, clauses: list[str]) -> tuple[Optional[tuple[str, str]],
                                                                    Optional[tuple[str, str]]]:
    """(phrase, language) of the first un-negated match, and of the first negated one."""
    negated = None
    for lang, phrases in phrases_by_lang.items():
        for phrase in phrases:
            for clause in clauses:
                for is_negated in _occurrences(phrase, clause):
                    if is_negated and lang in NEGATION_LANGUAGES:
                        negated = negated or (phrase, lang)
                    else:
                        return (phrase, lang), None
    return None, negated


def _first_match(phrases_by_lang: dict, normalised_text: str) -> Optional[tuple[str, str]]:
    """Return (phrase, language) for the first matching phrase, or None."""
    for lang, phrases in phrases_by_lang.items():
        for phrase in phrases:
            if _find_phrase(phrase, normalised_text):
                return phrase, lang
    return None


def check_red_flags(intake: PatientIntake, rules: dict | None = None) -> RulesResult:
    """Check the patient's text (and age/pregnancy) against every red-flag rule."""
    return check_red_flags_for(intake.symptoms_text, intake.age, intake.is_pregnant, rules)


def check_red_flags_for(symptoms_text: str, age: int, pregnant: bool,
                        rules: dict | None = None) -> RulesResult:
    """Same as check_red_flags, from plain values (used again after a positive pregnancy test)."""
    rules = rules or load_rules()
    text = normalise_text(symptoms_text)
    clauses = _clauses(symptoms_text)

    # Pregnancy counts if the form/test says so OR the patient mentions it in their text.
    # (Deliberately NOT negated: "not sure if I'm pregnant" must still count – safe side.)
    pregnancy_in_text = _first_match(rules.get("pregnancy_context_phrases", {}), text)
    is_pregnant = pregnant or pregnancy_in_text is not None

    matches: list[RuleMatch] = []
    negated: list[RuleMatch] = []
    for rule in rules["rules"]:
        if rule.get("requires_pregnancy") and not is_pregnant:
            continue
        if "max_age" in rule and age > rule["max_age"]:
            continue

        found, found_negated = _match_rule(rule["phrases"], clauses)
        for hit, bucket in ((found, matches), (found_negated, negated)):
            if hit:
                phrase, lang = hit
                bucket.append(
                    RuleMatch(
                        rule_id=rule["id"],
                        rule_name=rule["name"],
                        urgency=Urgency(rule["urgency"]),
                        matched_phrase=phrase,
                        language=lang,
                    )
                )

    # The overall rules result is the MOST urgent rule that matched.
    urgency: Optional[Urgency] = None
    for m in matches:
        urgency = m.urgency if urgency is None else max_urgency(urgency, m.urgency)

    return RulesResult(matches=matches, urgency=urgency, negated=negated)
