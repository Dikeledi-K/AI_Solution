"""SMS notifications.

Right now ClinicQ only SIMULATES SMS: MockSMSService prints each message and
appends it to data/sms_outbox.log. No real messages are sent.

To plug in a real gateway later:
    1. Finish ClickatellSMSService.send() (see the TODO there).
    2. Set SMS_PROVIDER=clickatell and CLICKATELL_API_KEY=... in .env
Nothing else in the app needs to change, because every service has the same
send(to, message) method.

Privacy: SMS text never contains symptoms or urgency – only the queue number.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from core.privacy import mask_phone

load_dotenv()
logger = logging.getLogger("clinicq.sms")

DEFAULT_OUTBOX = Path(__file__).resolve().parent.parent / "data" / "sms_outbox.log"


class SMSResult(BaseModel):
    success: bool
    provider: str
    to_masked: str
    message: str
    error: str | None = None


class SMSService(ABC):
    name = "base"

    @abstractmethod
    def send(self, to: str, message: str) -> SMSResult:
        """Send one SMS. Must never raise – return SMSResult(success=False) instead."""


class MockSMSService(SMSService):
    """Logs messages to the console and to a file instead of sending them."""

    name = "mock"

    def __init__(self, outbox_path: Path | None = None):
        self.outbox_path = Path(outbox_path or os.getenv("SMS_OUTBOX_PATH", DEFAULT_OUTBOX))

    def send(self, to: str, message: str) -> SMSResult:
        masked = mask_phone(to)
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | to {masked} | {message}"
        print(f"[MOCK SMS] {line}")
        logger.info("[MOCK SMS] %s", line)
        try:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.outbox_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            return SMSResult(success=False, provider=self.name, to_masked=masked, message=message, error=str(e))
        return SMSResult(success=True, provider=self.name, to_masked=masked, message=message)

    def recent_messages(self, limit: int = 10) -> list[str]:
        """Read the last few simulated messages (newest first) for the dashboard."""
        if not self.outbox_path.exists():
            return []
        lines = self.outbox_path.read_text(encoding="utf-8").strip().splitlines()
        return list(reversed(lines[-limit:]))

    def clear(self) -> None:
        if self.outbox_path.exists():
            self.outbox_path.unlink()


class ClickatellSMSService(SMSService):
    """Placeholder for the real Clickatell gateway. NOT implemented on purpose.

    The prototype must never send real SMS. When the clinic is ready, implement
    send() with an HTTPS POST to Clickatell's messaging API using self.api_key,
    following Clickatell's current documentation.
    """

    name = "clickatell"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def send(self, to: str, message: str) -> SMSResult:
        # TODO: implement the real Clickatell API call here.
        return SMSResult(
            success=False, provider=self.name, to_masked=mask_phone(to), message=message,
            error="ClickatellSMSService is a stub – no real SMS sent.",
        )


def get_sms_service() -> SMSService:
    """Choose the SMS service from SMS_PROVIDER (default: mock)."""
    provider = os.getenv("SMS_PROVIDER", "mock").strip().lower()
    if provider == "clickatell":
        api_key = os.getenv("CLICKATELL_API_KEY")
        if api_key:
            return ClickatellSMSService(api_key)
        logger.warning("SMS_PROVIDER=clickatell but CLICKATELL_API_KEY missing – using mock SMS.")
    return MockSMSService()


# Message templates – queue number only, no medical details.
def near_front_message(queue_number: int) -> str:
    return (f"ClinicQ: Queue no. {queue_number} – you will be called soon. "
            "Please stay close to the waiting area.")


def called_message(queue_number: int, room: str | None = None) -> str:
    return f"ClinicQ: Queue no. {queue_number} – please come to {room or 'the nurse'} now."
