# 🏥 ClinicQ — AI Patient Intake, Triage & Clinic Flow

**Live demo:** https://nztdbvtrm3ve2ixwknarwg.streamlit.app/

ClinicQ helps South African public clinics see the most urgent patients first. Patients describe their
symptoms in English, isiZulu, Sesotho, Sepedi or Afrikaans; danger-sign rules and an AI assistant suggest a
triage colour; and staff move each patient through vitals, consultation, HIV testing, injection and pharmacy.
**Staff make every clinical decision.**

> ⚠️ ClinicQ supports clinic staff and does not diagnose. It is a prototype: use demo data only.

| | |
|---|---|
| **Stack** | Python, Streamlit, Pydantic, SQLite, Claude / Gemini / Groq (or offline mock) |
| **Safety** | Rules can only raise urgency, the AI never sees names or ID numbers, nurses confirm every colour |
| **Security** | Staff ID + hashed PIN, roles, lockout, auto sign-out, full audit log |
| **Tests** | 164 automated tests (`pytest`) |

## Where things are

The app lives in the [`clinicq/`](clinicq/) folder:

- [`clinicq/README.md`](clinicq/README.md): full documentation, setup and the 5-minute demo script
- [`clinicq/RESPONSIBLE_AI.md`](clinicq/RESPONSIBLE_AI.md): risks and how ClinicQ addresses them

## Run it locally

```bash
cd clinicq
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Built by [Dikeledi Kwenaite](https://dk-portfolio-chronicle.lovable.app/).
