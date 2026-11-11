# -*- coding: utf-8 -*-
"""
CTV (Collaborator / Cộng tác viên) referral code & slug resolution helpers.
"""

import re
import hashlib
from typing import Optional, List
from core.state import FORM_REGISTRY
from services.form_engine import FormSchema

CTV_CODE_MAP = {
    "ctv001": "CTV001_Dũng_HN",
    "ctv002": "CTV002_Tuấn_HN",
    "ctv003": "CTV003_Ngân_SG",
    "ctv004": "CTV004_Vỹ_SG",
    "ctv005": "CTV005_Hương_DN",
}


def get_deterministic_ctv_slug(ctv_id: str, form_base: str = "income-survey") -> str:
    """Deterministically generates a slug from CTV ID."""
    clean_ctv = ctv_id.strip()
    h = hashlib.sha256(f"tb_ctv_salt_2026:{clean_ctv}".encode("utf-8")).hexdigest()[:14]
    return f"{form_base}-{h}"


def resolve_ctv_from_slug(slug: str) -> Optional[str]:
    """Resolves CTV ID from known CTV choices or registered options."""
    schema = FORM_REGISTRY.get("income_survey") or next(iter(FORM_REGISTRY.values()), None)
    if not schema:
        return None
    ctv_q = next((q for q in schema.questions if q.id in ("ctv_code", "collaborator_id")), None)
    ctv_options = ctv_q.options if ctv_q and ctv_q.options else list(CTV_CODE_MAP.values())
    for opt in ctv_options:
        for base in ("income-survey", "income_survey"):
            if get_deterministic_ctv_slug(opt, base) == slug:
                return opt
    return None


def extract_ctv_short_code(ctv_id: str) -> str:
    """Extracts short ref code (e.g. 'ctv001') from full CTV ID or raw code."""
    clean = ctv_id.strip().lower()
    m = re.match(r"(ctv\d+)", clean)
    if m:
        return m.group(1)
    parts = clean.split("_")
    if parts and parts[0].startswith("ctv"):
        return parts[0]
    return clean


def resolve_ctv_identifier(ref: str) -> Optional[str]:
    """Resolves short code (e.g. 'ctv001'), slug, or full CTV name to authoritative CTV ID."""
    if not ref:
        return None
    clean = ref.strip()
    short = extract_ctv_short_code(clean)
    if short in CTV_CODE_MAP:
        return CTV_CODE_MAP[short]
    for k, v in CTV_CODE_MAP.items():
        if k == clean.lower() or v.lower() == clean.lower():
            return v
    return clean


def get_ctv_options_for_form(schema: FormSchema) -> List[str]:
    """Retrieves list of CTV options from schema or fallback list."""
    for q in schema.questions:
        if q.id in ("ctv_code", "collaborator_id") and q.options:
            return q.options
    return list(CTV_CODE_MAP.values())
