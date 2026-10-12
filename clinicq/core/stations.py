"""The stations a patient moves through during a clinic visit.

    Registration -> VITALS -> CLINICIAN -> (HIV_TEST -> INJECTION -> PHARMACY, as needed) -> home

Registration is the Patient Intake page. The separate "history-taking"
consultation is skipped because the patient already describes their problem
at registration.
"""

STATIONS = {
    "VITALS": {"label": "Vitals & tests", "icon": "🩺",
               "help": "BP, temperature, pulse, urine infection & pregnancy tests"},
    "CLINICIAN": {"label": "Nurse / Doctor", "icon": "👩‍⚕️",
                  "help": "Assessment and prescription"},
    # Listed in the order patients visit them.
    "HIV_TEST": {"label": "HIV testing", "icon": "🧪", "help": "HIV counselling and testing"},
    "INJECTION": {"label": "Injection room", "icon": "💉", "help": "Injections and vaccinations"},
    "PHARMACY": {"label": "Pharmacy", "icon": "💊", "help": "Collect medication"},
}

# Rooms the Nurse/Doctor can refer a patient on to. The patient ALWAYS visits them in
# this order: HIV testing first, then the injection room, then the pharmacy last
# (so medication is collected at the end, after any test results are known).
AFTER_CLINICIAN = ["HIV_TEST", "INJECTION", "PHARMACY"]

# Overdue targets (SATS) are about the wait to be SEEN, so they apply only here.
TRIAGE_STATIONS = {"VITALS", "CLINICIAN"}


def label(station: str | None) -> str:
    if not station:
        return "—"
    info = STATIONS.get(station)
    return f"{info['icon']} {info['label']}" if info else station
