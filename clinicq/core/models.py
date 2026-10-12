"""Pydantic data models for ClinicQ.

Pydantic checks that data has the right shape and types. If the LLM returns
something unexpected (missing field, wrong urgency word, severity of 42...),
validation fails and the triage pipeline falls back safely.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.privacy import age_on, normalise_phone, redact_id_numbers, sa_id_birth_date


# ---------------------------------------------------------------------------
# Urgency levels
# ---------------------------------------------------------------------------
class Urgency(str, Enum):
    """South African Triage Scale-style colours (simplified)."""

    RED = "RED"  # Emergency – see immediately
    ORANGE = "ORANGE"  # Very urgent
    YELLOW = "YELLOW"  # Urgent
    GREEN = "GREEN"  # Routine

    @property
    def rank(self) -> int:
        """Higher number = more urgent. Used for comparing and sorting."""
        return URGENCY_RANK[self]


URGENCY_RANK = {Urgency.GREEN: 1, Urgency.YELLOW: 2, Urgency.ORANGE: 3, Urgency.RED: 4}


def max_urgency(a: Urgency, b: Urgency) -> Urgency:
    """Return the MORE urgent of two levels (RED beats ORANGE beats YELLOW beats GREEN)."""
    return a if a.rank >= b.rank else b


CHRONIC_CONDITIONS = ["diabetes", "hypertension", "HIV", "TB", "asthma", "epilepsy", "none"]


# ---------------------------------------------------------------------------
# Patient intake (what the patient/clerk types into the form)
# ---------------------------------------------------------------------------
ID_TYPES = ["SA ID", "Passport", "Asylum/refugee permit", "No document"]


def _valid_sa_phone(value: Optional[str]) -> Optional[str]:
    if value is None or value.strip() == "":
        return None
    digits = normalise_phone(value)
    if not re.fullmatch(r"0\d{9}", digits):
        raise ValueError("Phone number must be a 10-digit SA number, e.g. 0821234567.")
    return digits


class PatientIntake(BaseModel):
    # --- Personal details (stored for the clinic's records, NEVER sent to the AI) ---
    first_name: str = Field(min_length=1, max_length=40)
    surname: str = Field(min_length=1, max_length=40)
    # Everyone may use a public clinic, with or without an SA ID, so the ID is optional.
    id_type: Literal["SA ID", "Passport", "Asylum/refugee permit", "No document"] = "No document"
    id_number: Optional[str] = None
    address: str = Field(min_length=3, max_length=200)
    phone: Optional[str] = None
    nok_name: str = Field(min_length=1, max_length=80, description="Next of kin")
    nok_relationship: str = Field(min_length=1, max_length=40)
    nok_phone: str
    consent: bool = Field(description="Patient agreed that the clinic stores their details (POPIA)")

    # --- Clinical details (age, sex and pregnancy are shared with the AI) ---
    age: int = Field(ge=0, le=120)
    sex: Literal["Female", "Male", "Other"]
    pregnant: Literal["Yes", "No", "N/A"] = "N/A"
    chronic_conditions: list[str] = Field(default_factory=list)
    symptoms_text: str = Field(min_length=3, max_length=2000)

    @field_validator("first_name", "surname", "nok_name")
    @classmethod
    def letters_only(cls, value: str) -> str:
        value = value.strip()
        if re.search(r"\d", value):
            raise ValueError("Names cannot contain numbers.")
        if not value:
            raise ValueError("Please enter a name.")
        return value

    @field_validator("address", "nok_relationship")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field is required.")
        return value

    @field_validator("consent")
    @classmethod
    def must_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("The patient must agree that the clinic stores their details.")
        return value

    @field_validator("nok_phone")
    @classmethod
    def valid_nok_phone(cls, value: str) -> str:
        phone = _valid_sa_phone(value)
        if phone is None:
            raise ValueError("Please enter the next of kin's phone number.")
        return phone

    @model_validator(mode="after")
    def check_id_number(self) -> "PatientIntake":
        number = (self.id_number or "").replace(" ", "").upper()
        if self.id_type == "No document":
            self.id_number = None
            return self
        if not number:
            raise ValueError(f"Please enter the {self.id_type} number (or choose 'No document').")
        if self.id_type == "SA ID":
            birth = sa_id_birth_date(number)  # raises ValueError with a clear message
            if abs(age_on(birth) - self.age) > 1:
                raise ValueError(f"The age entered ({self.age}) does not match the SA ID number "
                                 f"(born {birth:%d %b %Y}, age {age_on(birth)}).")
        elif not re.fullmatch(r"[A-Z0-9-]{5,20}", number):
            raise ValueError(f"{self.id_type} number should be 5–20 letters or digits.")
        self.id_number = number
        return self

    @property
    def date_of_birth(self) -> Optional[str]:
        if self.id_type == "SA ID" and self.id_number:
            return sa_id_birth_date(self.id_number).isoformat()
        return None

    @field_validator("chronic_conditions")
    @classmethod
    def known_conditions_only(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in CHRONIC_CONDITIONS]
        if unknown:
            raise ValueError(f"Unknown chronic condition(s): {unknown}")
        # "none" together with real conditions makes no sense – drop "none".
        if "none" in values and len(values) > 1:
            values = [v for v in values if v != "none"]
        return values

    @field_validator("phone")
    @classmethod
    def valid_sa_phone(cls, value: Optional[str]) -> Optional[str]:
        return _valid_sa_phone(value)

    @field_validator("symptoms_text")
    @classmethod
    def strip_id_numbers(cls, value: str) -> str:
        return redact_id_numbers(value.strip())

    @model_validator(mode="after")
    def pregnancy_makes_sense(self) -> "PatientIntake":
        if self.sex == "Male" and self.pregnant == "Yes":
            raise ValueError("Pregnant cannot be 'Yes' when sex is Male.")
        return self

    @property
    def is_pregnant(self) -> bool:
        return self.pregnant == "Yes"


# ---------------------------------------------------------------------------
# Vital signs and urine tests (captured at the Vitals station)
# ---------------------------------------------------------------------------
TestResult = Literal["Positive", "Negative", "Not done"]


class Vitals(BaseModel):
    bp_systolic: int = Field(ge=50, le=260, description="mmHg")
    bp_diastolic: int = Field(ge=30, le=160, description="mmHg")
    temperature: float = Field(ge=30.0, le=44.0, description="°C")
    pulse: Optional[int] = Field(default=None, ge=20, le=250, description="beats per minute")
    urine_infection: TestResult = "Not done"   # urine dipstick: leucocytes / nitrites
    urine_pregnancy: Literal["Positive", "Negative", "Not done", "Not applicable"] = "Not applicable"

    @model_validator(mode="after")
    def diastolic_below_systolic(self) -> "Vitals":
        if self.bp_diastolic >= self.bp_systolic:
            raise ValueError("Diastolic BP must be lower than systolic BP.")
        return self

    def summary(self) -> str:
        pulse = f", pulse {self.pulse}" if self.pulse else ""
        return (f"BP {self.bp_systolic}/{self.bp_diastolic}, temp {self.temperature:.1f}°C{pulse}; "
                f"urine infection: {self.urine_infection}; pregnancy test: {self.urine_pregnancy}")


class VitalsFlag(BaseModel):
    name: str
    urgency: Urgency


# ---------------------------------------------------------------------------
# Rules engine output
# ---------------------------------------------------------------------------
class RuleMatch(BaseModel):
    rule_id: str
    rule_name: str
    urgency: Urgency
    matched_phrase: str
    language: str


class RulesResult(BaseModel):
    matches: list[RuleMatch] = Field(default_factory=list)
    urgency: Optional[Urgency] = None  # None = no red flag found

    @property
    def triggered(self) -> bool:
        return self.urgency is not None

    def summary(self) -> str:
        if not self.matches:
            return "No red-flag rules matched."
        names = sorted({m.rule_name for m in self.matches})
        return "Red-flag rule(s) matched: " + ", ".join(names)


# ---------------------------------------------------------------------------
# LLM output – this is the STRICT JSON contract the LLM must follow
# ---------------------------------------------------------------------------
class LLMTriageOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    detected_language: str
    english_translation: str
    chief_complaint: str
    symptoms: list[str]
    duration: Optional[str] = None
    self_reported_severity: Optional[int] = Field(default=None, ge=1, le=10)
    red_flags_mentioned: list[str]
    suggested_urgency: Urgency
    reasoning: str
    questions_for_nurse: list[str]

    @field_validator("suggested_urgency", mode="before")
    @classmethod
    def normalise_urgency(cls, value):
        # Accept "red" or " Red " but still reject anything that is not a real level.
        if isinstance(value, str):
            return value.strip().upper()
        return value


# ---------------------------------------------------------------------------
# Final triage result (rules + LLM combined)
# ---------------------------------------------------------------------------
DecidedBy = Literal["rules", "ai", "rules + ai (agree)", "fallback", "rules + fallback (agree)"]


class TriageResult(BaseModel):
    final_urgency: Urgency
    decided_by: DecidedBy
    rules_result: RulesResult
    ai_urgency: Urgency  # the AI suggestion (or YELLOW if the AI failed)
    llm_output: Optional[LLMTriageOutput] = None
    llm_error: Optional[str] = None  # filled in when the AI failed and we fell back
    llm_attempts: int = 0
    llm_provider: str = "unknown"

    @property
    def needs_manual_review(self) -> bool:
        return self.llm_output is None

    def explanation(self) -> str:
        """A plain-language 'why' for the nurse dashboard."""
        parts = []
        if self.rules_result.triggered:
            parts.append(self.rules_result.summary())
        if self.llm_output:
            parts.append(f"AI: {self.llm_output.reasoning}")
        else:
            parts.append("AI could not structure this entry – YELLOW, needs manual review.")
        return " ".join(parts)
