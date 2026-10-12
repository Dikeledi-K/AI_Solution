"""Vital-sign danger checks, run when the Vitals station saves BP, temperature etc.

Like the red-flag rules, these can only RAISE urgency, never lower it, and the
nurse confirms the final colour.

IMPORTANT: the thresholds below are reasonable EXAMPLES for a prototype. The
clinic's clinical team must review and approve them (for example against the
South African Triage Scale's TEWS score) before real use.
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


def check_vitals(vitals: Vitals, pregnant: bool) -> list[VitalsFlag]:
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

    return flags


def flags_urgency(flags: list[VitalsFlag]) -> Optional[Urgency]:
    """The most urgent level among the flags, or None if there are none."""
    result: Optional[Urgency] = None
    for f in flags:
        result = f.urgency if result is None else max_urgency(result, f.urgency)
    return result
