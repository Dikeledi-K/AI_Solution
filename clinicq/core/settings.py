"""Deployment settings read from environment variables (or .env / Streamlit secrets).

    CLINICQ_DEMO_MODE=1      Classroom demo: demo staff accounts (public PINs), demo patients,
                             "Simulate AI failure" and "Reset demo data" are available.
                             NEVER turn this on for a real clinic.
    CLINICQ_SETUP_CODE=...   One-time code needed to create the first administrator on a
                             real (non-demo) system. Without it, first-time setup is disabled,
                             so a stranger who reaches a fresh install cannot make themselves admin.

On Streamlit Community Cloud, top-level secrets are also exposed as environment
variables, so these can be set under App settings -> Secrets.

Values are read on every call (not cached) so tests can change them.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

_TRUE = {"1", "true", "yes", "on"}


def demo_mode() -> bool:
    return os.getenv("CLINICQ_DEMO_MODE", "0").strip().lower() in _TRUE


def setup_code() -> str | None:
    code = (os.getenv("CLINICQ_SETUP_CODE") or "").strip()
    return code or None
