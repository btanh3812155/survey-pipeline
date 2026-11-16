# -*- coding: utf-8 -*-
"""
CTV (Collaborator / Cộng tác viên) referral code & slug resolution helpers.

Codes are stored as canonical uppercase ASCII: diacritics fold to a-z, spaces and
punctuation collapse to underscores, e.g. "Tuấn_LM_HCM_001" -> "TUAN_LM_HCM_001".
"""

import re
import hashlib
import unicodedata
from typing import List, Optional

from core.state import FORM_REGISTRY
from services.form_engine import FormSchema

CTV_CODES = (
    "TUAN_LM_HCM_001",
    "DUNG_NV_HCM_002",
    "KHUYEN_TL_HCM_003",
    "VY_PV_HCM_004",
)

# Canonical code -> canonical code. Kept as a map for callers that iterate it.
CTV_CODE_MAP = {code: code for code in CTV_CODES}


def normalize_ctv_code(value: str) -> str:
    """Fold Vietnamese diacritics to ASCII, collapse separators, uppercase."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # đ/Đ does not decompose under NFKD.
    ascii_only = ascii_only.replace("đ", "d").replace("Đ", "D")
    ascii_only = ascii_only.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    return cleaned.upper()


def get_deterministic_ctv_slug(ctv_id: str, form_base: str = "income-survey") -> str:
    """Deterministically generates a slug from a canonical CTV code."""
    clean_ctv = normalize_ctv_code(ctv_id)
    h = hashlib.sha256(f"tb_ctv_salt_2026:{clean_ctv}".encode("utf-8")).hexdigest()[:14]
    return f"{form_base}-{h}"


def resolve_ctv_from_slug(slug: str) -> Optional[str]:
    """Resolves a CTV code from a deterministic slug."""
    for code in CTV_CODES:
        for base in ("income-survey", "income_survey"):
            if get_deterministic_ctv_slug(code, base) == slug:
                return code
    return None


def extract_ctv_short_code(ctv_id: str) -> str:
    """Returns the canonical ref code for any CTV name/code input."""
    return normalize_ctv_code(ctv_id)


def resolve_ctv_identifier(ref: str) -> Optional[str]:
    """Resolves a ref/slug/CTV name to the canonical uppercase code."""
    if not ref:
        return None
    code = normalize_ctv_code(ref)
    if code in CTV_CODE_MAP:
        return CTV_CODE_MAP[code]
    slug_resolved = resolve_ctv_from_slug(ref.strip())
    if slug_resolved:
        return slug_resolved
    return code or None


def get_ctv_options_for_form(schema: FormSchema) -> List[str]:
    """Returns the form's CTV options, or the registry when the form has none."""
    for q in schema.questions:
        if q.id in ("ctv_code", "collaborator_id") and q.options:
            return [normalize_ctv_code(str(opt)) for opt in q.options]
    return list(CTV_CODES)
