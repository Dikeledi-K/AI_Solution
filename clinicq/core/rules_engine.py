"""Red-flag rules engine – runs FIRST, before any AI.

Why rules before AI?
    An LLM can mistranslate, misunderstand or simply be unavailable. Danger signs
    such as chest pain or a child who is not drinking must ALWAYS be caught, so we
    check them with simple, transparent keyword rules that clinic staff can read
    and edit in data/red_flags.json.

Rules can only RAISE urgency. They return ORANGE or RED, or None (no opinion).
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

    # Pregnancy counts if the form/test says so OR the patient mentions it in their text.
    pregnancy_in_text = _first_match(rules.get("pregnancy_context_phrases", {}), text)
    is_pregnant = pregnant or pregnancy_in_text is not None

    matches: list[RuleMatch] = []
    for rule in rules["rules"]:
        if rule.get("requires_pregnancy") and not is_pregnant:
            continue
        if "max_age" in rule and age > rule["max_age"]:
            continue

        found = _first_match(rule["phrases"], text)
        if found:
            phrase, lang = found
            matches.append(
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

    return RulesResult(matches=matches, urgency=urgency)
