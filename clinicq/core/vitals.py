"""Vital-sign danger checks, run when the Vitals station saves BP, temperature etc.

Like the red-flag rules, these can only RAISE urgency, never lower it, and the
nurse confirms the final colour.

For adults and children over 12 the adult SATS TEWS (Triage Early Warning Score)
is calculated when all seven parts are recorded: mobility, respiratory rate, heart
rate, systolic BP, temperature, AVPU (level of consciousness) and trauma.
TEWS 7+ = RED, 5–6 = ORANGE, 3–4 = YELLOW, 0–2 = GREEN. Children 12 and under use
different (paediatric) TEWS charts, which are not automated yet, so for them only
the single danger thresholds below apply – and their weight must be recorded.

IMPORTANT: the thresholds below are reasonable EXAMPLES for a prototype. The
clinic's clinical team must review and approve them (and the TEWS table) against
the current South African Triage Scale manual before real use.
"""

from __future__ import annotations

from typing import Optional

from core.models import Urgency, Vitals, VitalsFlag, max_urgency

# --- Thresholds (edit with clinical approval) -------------------------------
SYSTOLIC_SHOCK = 90          # systolic below this            -> RED
SYSTOLIC_VERY_HIGH = 180     # systolic at/above this         -> ORANGE
DIASTOLIC_VERY_HIGH = 110    # diastolic at/above this        -> ORANGE
PREGNANCY_SYSTOLIC_HIGH = 140   # in pregnancy, at/above      -> ORANGE (pre-eclampsia risk)
PREGNANCY_DIASTOLIC_HIGH = 90
TEMP_VERY_HIGH = 40.0        # at/above                       -> RED
TEMP_HIGH = 39.0             # at/above                       -> ORANGE
TEMP_LOW = 35.0              # below                          -> RED
PULSE_VERY_HIGH = 130        # above                          -> RED
PULSE_HIGH = 110             # above                          -> ORANGE
PULSE_LOW = 40               # below                          -> RED
SPO2_LOW = 92                # oxygen saturation below        -> ORANGE
GLUCOSE_LOW = 3.0            # mmol/L, below (hypoglycaemia)  -> ORANGE
GLUCOSE_HIGH = 11.0          # mmol/L, above (hyperglycaemia) -> YELLOW
ADULT_TEWS_FROM_AGE = 13     # younger children need the paediatric TEWS chart


# --- Adult SATS TEWS ----------------------------------------------------------
def _score_rr(rr: int) -> int:
    return 3 if rr < 9 else 0 if rr <= 14 else 1 if rr <= 20 else 2 if rr <= 29 else 3


def _score_hr(hr: int) -> int:
    return 3 if hr < 41 else 1 if hr <= 50 else 0 if hr <= 100 else 1 if hr <= 110 else 2 if hr <= 129 else 3


def _score_sbp(sbp: int) -> int:
    return 3 if sbp < 71 else 2 if sbp <= 80 else 1 if sbp <= 100 else 0 if sbp <= 199 else 2


def _score_temp(temp: float) -> int:
    return 2 if temp < 35.0 or temp > 38.4 else 0


MOBILITY_SCORE = {"Walking": 0, "With help": 1, "Stretcher / immobile": 2}
AVPU_SCORE = {"Alert": 0, "Confused": 1, "Reacts to voice": 1, "Reacts to pain": 2, "Unresponsive": 3}


def tews_score(vitals: Vitals, age: Optional[int]) -> Optional[dict]:
    """Adult TEWS: {"score": n, "urgency": Urgency, "parts": {...}}.

    None if the patient is 12 or younger, or any of the seven parts was not recorded.
    """
    if age is None or age < ADULT_TEWS_FROM_AGE:
        return None
    needed = (vitals.mobility, vitals.resp_rate, vitals.pulse, vitals.avpu, vitals.trauma)
    if any(v is None for v in needed):
        return None
    parts = {
        "mobility": MOBILITY_SCORE[vitals.mobility],
        "resp rate": _score_rr(vitals.resp_rate),
        "heart rate": _score_hr(vitals.pulse),
        "systolic BP": _score_sbp(vitals.bp_systolic),
        "temperature": _score_temp(vitals.temperature),
        "AVPU": AVPU_SCORE[vitals.avpu],
        "trauma": int(bool(vitals.trauma)),
    }
    score = sum(parts.values())
    urgency = (Urgency.RED if score >= 7 else Urgency.ORANGE if score >= 5
               else Urgency.YELLOW if score >= 3 else Urgency.GREEN)
    return {"score": score, "urgency": urgency, "parts": parts}


def check_vitals(vitals: Vitals, pregnant: bool, age: Optional[int] = None) -> list[VitalsFlag]:
    """Return every danger flag the vital signs raise (empty list = nothing abnormal)."""
    flags: list[VitalsFlag] = []

    def flag(name: str, urgency: Urgency) -> None:
        flags.append(VitalsFlag(name=name, urgency=urgency))

    if vitals.bp_systolic < SYSTOLIC_SHOCK:
        flag(f"Low blood pressure ({vitals.bp_systolic}/{vitals.bp_diastolic})", Urgency.RED)
    elif vitals.bp_systolic >= SYSTOLIC_VERY_HIGH or vitals.bp_diastolic >= DIASTOLIC_VERY_HIGH:
        flag(f"Very high blood pressure ({vitals.bp_systolic}/{vitals.bp_diastolic})", Urgency.ORANGE)
    elif pregnant and (vitals.bp_systolic >= PREGNANCY_SYSTOLIC_HIGH
                       or vitals.bp_diastolic >= PREGNANCY_DIASTOLIC_HIGH):
        flag(f"High blood pressure in pregnancy ({vitals.bp_systolic}/{vitals.bp_diastolic})",
             Urgency.ORANGE)

    if vitals.temperature >= TEMP_VERY_HIGH:
        flag(f"Very high temperature ({vitals.temperature:.1f}°C)", Urgency.RED)
    elif vitals.temperature >= TEMP_HIGH:
        flag(f"High temperature ({vitals.temperature:.1f}°C)", Urgency.ORANGE)
    elif vitals.temperature < TEMP_LOW:
        flag(f"Low body temperature ({vitals.temperature:.1f}°C)", Urgency.RED)

    if vitals.pulse is not None:
        if vitals.pulse > PULSE_VERY_HIGH or vitals.pulse < PULSE_LOW:
            flag(f"Dangerous pulse ({vitals.pulse}/min)", Urgency.RED)
        elif vitals.pulse > PULSE_HIGH:
            flag(f"Fast pulse ({vitals.pulse}/min)", Urgency.ORANGE)

    if vitals.spo2 is not None and vitals.spo2 < SPO2_LOW:
        flag(f"Low oxygen saturation ({vitals.spo2}%)", Urgency.ORANGE)

    if vitals.glucose is not None:
        if vitals.glucose < GLUCOSE_LOW:
            flag(f"Low blood glucose ({vitals.glucose:.1f} mmol/L)", Urgency.ORANGE)
        elif vitals.glucose > GLUCOSE_HIGH:
            flag(f"High blood glucose ({vitals.glucose:.1f} mmol/L)", Urgency.YELLOW)

    if vitals.avpu == "Unresponsive":
        flag("Unresponsive (AVPU)", Urgency.RED)

    tews = tews_score(vitals, age)
    if tews and tews["score"] >= 3:
        flag(f"TEWS {tews['score']}", tews["urgency"])

    return flags


def flags_urgency(flags: list[VitalsFlag]) -> Optional[Urgency]:
    """The most urgent level among the flags, or None if there are none."""
    result: Optional[Urgency] = None
    for f in flags:
        result = f.urgency if result is None else max_urgency(result, f.urgency)
    return result


def vitals_text(record: dict) -> str:
    """One readable line for a stored vitals record (as saved by workflow.record_vitals)."""
    fields = {k: v for k, v in record.items() if k in Vitals.model_fields}
    text = Vitals(**fields).summary()
    if record.get("tews") is not None:
        text += f"; TEWS {record['tews']}"
    return text
