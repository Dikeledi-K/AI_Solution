"""Privacy helpers used across ClinicQ.

ClinicQ stores full patient details (name, ID number, address, next of kin) for
the clinic's records, so it must protect them:
- ID numbers and phone numbers are masked on every dashboard
- full details are only shown on the PIN-protected Patient Records page
- personal details are NEVER sent to the AI (only symptoms, age, sex, pregnancy)
- ID numbers typed into the free-text symptoms box are removed
"""

import re
from datetime import date

# A South African ID number is 13 digits. We never want to store one, even by accident
# (patients sometimes type it into the free-text box).
_SA_ID_PATTERN = re.compile(r"\b\d{13}\b")


def redact_id_numbers(text: str) -> str:
    """Replace anything that looks like a 13-digit SA ID number with a placeholder."""
    return _SA_ID_PATTERN.sub("[ID number removed]", text)


def normalise_phone(phone: str) -> str:
    """Turn '+27 82 123 4567' or '082-123-4567' into the local format '0821234567'."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("27") and len(digits) == 11:
        digits = "0" + digits[2:]
    return digits


def luhn_valid(number: str) -> bool:
    """Luhn checksum – the last digit of an SA ID number is a Luhn check digit."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def sa_id_birth_date(id_number: str, today: date | None = None) -> date:
    """Check a 13-digit SA ID number and return the date of birth inside it.

    Format: YYMMDD SSSS C A Z (birth date, sequence, citizenship, A, checksum).
    Raises ValueError if the number is not a valid SA ID.
    """
    today = today or date.today()
    if not re.fullmatch(r"\d{13}", id_number):
        raise ValueError("An SA ID number has exactly 13 digits.")
    if not luhn_valid(id_number):
        raise ValueError("This SA ID number is not valid (checksum failed) – please check it.")
    yy, mm, dd = int(id_number[0:2]), int(id_number[2:4]), int(id_number[4:6])
    century = 2000 if 2000 + yy <= today.year else 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        raise ValueError("The date of birth inside this SA ID number is not a real date.") from None


def age_on(birth: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def mask_id(id_number: str | None) -> str:
    """Show only the last 4 characters of an ID/passport number, e.g. '*********0087'."""
    if not id_number:
        return "—"
    return "*" * max(len(id_number) - 4, 3) + id_number[-4:]


def mask_phone(phone: str | None) -> str:
    """Mask a phone number for display, e.g. '0821234567' -> '082***4567'."""
    if not phone:
        return "—"
    digits = normalise_phone(phone)
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}***{digits[-4:]}"
