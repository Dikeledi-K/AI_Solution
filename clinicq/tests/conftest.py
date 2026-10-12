"""Shared pytest fixtures."""

import pytest

from core.models import PatientIntake


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Every test gets its own empty database and SMS outbox, and always uses the mock LLM."""
    monkeypatch.setenv("CLINICQ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SMS_OUTBOX_PATH", str(tmp_path / "sms.log"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("SMS_PROVIDER", "mock")


@pytest.fixture
def make_intake():
    """Build a PatientIntake with sensible defaults; override any field by keyword."""

    def _make(symptoms_text: str, **overrides) -> PatientIntake:
        data = {
            "first_name": "Test",
            "surname": "Patient",
            "id_type": "No document",
            "address": "1 Test Street, Soweto",
            "nok_name": "Test Relative",
            "nok_relationship": "Sister",
            "nok_phone": "0829990000",
            "consent": True,
            "age": 35,
            "sex": "Female",
            "pregnant": "N/A",
            "chronic_conditions": [],
            "phone": None,
            "symptoms_text": symptoms_text,
        }
        data.update(overrides)
        return PatientIntake(**data)

    return _make
