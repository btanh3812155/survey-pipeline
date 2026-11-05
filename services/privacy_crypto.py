"""
Privacy Crypto & PII Pseudonymization Service (Decree 13 / GDPR Compliance).
Provides deterministic HMAC-SHA256 hashing, masking, and driver tag generation.
"""

import hmac
import hashlib
import re
import os
from typing import Optional
from config import settings

FALLBACK_PEPPER = "survey_secret_salt_pepper_survey_pipeline_2026"


def get_secret_pepper() -> bytes:
    """Reads secret pepper from settings or environment with deterministic fallback."""
    pepper = getattr(settings, "APP_SECRET", None)
    if not pepper:
        pepper = os.environ.get("APP_SECRET", FALLBACK_PEPPER)
    return str(pepper).encode("utf-8")


def normalize_phone_for_hashing(phone: Optional[str]) -> str:
    """Normalizes phone string by stripping whitespace, hyphens, dots, and parens."""
    if not phone:
        return ""
    p = re.sub(r"[\s\-\.\(\)]", "", str(phone).strip())
    if p.startswith("+84"):
        p = "0" + p[3:]
    elif p.startswith("84") and len(p) >= 11:
        p = "0" + p[2:]
    return p


def normalize_plate_for_hashing(plate: Optional[str]) -> str:
    """Normalizes license plate to uppercase alphanumeric only."""
    if not plate:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(plate).upper().strip())


def hash_phone(phone: Optional[str]) -> str:
    """Computes deterministic HMAC-SHA256 hex digest for a driver's phone number."""
    if not phone:
        return ""
    norm = normalize_phone_for_hashing(phone)
    if not norm:
        return ""
    return hmac.new(get_secret_pepper(), norm.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_plate(plate: Optional[str]) -> str:
    """Computes deterministic HMAC-SHA256 hex digest for a vehicle license plate."""
    if not plate:
        return ""
    norm = normalize_plate_for_hashing(plate)
    if not norm:
        return ""
    return hmac.new(get_secret_pepper(), norm.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_driver_tag(phone: Optional[str]) -> str:
    """
    Generates a pseudonymized driver identifier (e.g. 'DRV-' + first 8 characters of phone hash).
    Used as clean primary key across customer-facing workflows and analytics.
    """
    if not phone:
        return "DRV-ANON"
    h = hash_phone(phone)
    if not h:
        return "DRV-ANON"
    return f"DRV-{h[:8].upper()}"


def mask_phone(phone: Optional[str]) -> str:
    """
    Masks middle digits of phone number for privacy display (e.g. '098****456').
    """
    if not phone:
        return ""
    p = str(phone).strip()
    clean = re.sub(r"[^\d+]", "", p)
    if len(clean) >= 7:
        return clean[:3] + "****" + clean[-3:]
    return "****"


def mask_license_plate(plate: Optional[str]) -> str:
    """
    Masks the middle portion of a Vietnamese license plate (e.g. '29E-***.45').
    """
    if not plate:
        return ""
    p = re.sub(r"\s+", "", str(plate).strip().upper())
    if not p:
        return ""

    # Formatted with hyphen and dot, e.g. 29E-123.45 or 30A-999.99
    m = re.match(r"^([0-9]{2}[A-Z0-9]{1,3})-([0-9]{3,4})\.([0-9]{2})$", p)
    if m:
        return f"{m.group(1)}-***.{m.group(3)}"

    # Formatted with hyphen only, e.g. 29E-12345
    m2 = re.match(r"^([0-9]{2}[A-Z0-9]{1,3})-(.*)([0-9]{2})$", p)
    if m2:
        return f"{m2.group(1)}-***.{m2.group(3)}"

    # Normalized alphanumeric without hyphen
    # E.g. '29E12345' (2-digit province + 1 letter + 5 digits) -> '29E-***.45'
    m_car = re.match(r"^([0-9]{2}[A-Z])([0-9]{3})([0-9]{2})$", p)
    if m_car:
        return f"{m_car.group(1)}-***.{m_car.group(3)}"

    # E.g. '29AA12345' (2-digit province + 2 letters + 5 digits) -> '29AA-***.45'
    m_car2 = re.match(r"^([0-9]{2}[A-Z]{2})([0-9]{2,3})([0-9]{2})$", p)
    if m_car2:
        return f"{m_car2.group(1)}-***.{m_car2.group(3)}"

    # E.g. '29E112345' (2-digit province + 1 letter + 1 digit + 4-5 digits) -> '29E1-***.45'
    m_bike = re.match(r"^([0-9]{2}[A-Z][0-9])([0-9]{2,3})([0-9]{2})$", p)
    if m_bike:
        return f"{m_bike.group(1)}-***.{m_bike.group(3)}"
    if len(p) >= 6:
        return f"{p[:3]}-***.{p[-2:]}"
    elif len(p) >= 3:
        return f"{p[:2]}***{p[-1]}"
    return "***"
