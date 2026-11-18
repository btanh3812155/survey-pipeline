# -*- coding: utf-8 -*-
"""
CTV (Collaborator / Cộng tác viên) referral code & slug resolution helpers.

Codes are stored as canonical uppercase ASCII: diacritics fold to a-z, spaces and
punctuation collapse to underscores, e.g. "Tuấn_LM_HCM_001" -> "TUAN_LM_HCM_001".

The CTV directory lives in PostgreSQL (`ctv_directory`). A process-local cache is
loaded at startup / after edits so synchronous rendering paths (landing page,
form lists, QR links) keep working without awaiting a DB round-trip. When the DB
is unavailable or empty, the static CTV_CODES registry is used as a fallback.
"""

import re
import html
import hashlib
import logging
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.state import FORM_REGISTRY
from services.form_engine import FormSchema

logger = logging.getLogger("core.ctv")

CTV_CODES = (
    "TUAN_LM_HCM_001",
    "DUNG_NV_HCM_002",
    "KHUYEN_TL_HCM_003",
    "VY_PV_HCM_004",
)

# Canonical code -> canonical code. Kept as a map for callers that iterate it.
CTV_CODE_MAP = {code: code for code in CTV_CODES}

# Fallback display names for the seed rows (no PII beyond the code itself).
STATIC_CTV_NAMES = {
    "TUAN_LM_HCM_001": "Tuấn LM (HCM)",
    "DUNG_NV_HCM_002": "Dũng NV (HCM)",
    "KHUYEN_TL_HCM_003": "Khuyên TL (HCM)",
    "VY_PV_HCM_004": "Vy PV (HCM)",
}

CTV_TABLE_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ctv_directory (
        code TEXT PRIMARY KEY,
        display_name TEXT,
        phone TEXT,
        telegram_user_id BIGINT,
        is_active BOOLEAN DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS form_tags (
        form_id TEXT NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (form_id, tag)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ctv_form_links (
        ctv_code TEXT NOT NULL,
        form_id TEXT NOT NULL,
        url TEXT,
        qr TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (ctv_code, form_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ctv_quotas (
        id SERIAL PRIMARY KEY,
        ctv_code TEXT NOT NULL,
        scope_type TEXT CHECK (scope_type IN ('form', 'tag', 'global')),
        scope_value TEXT,
        max_submissions INTEGER,
        period_days INTEGER,
        active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_ctv_directory_telegram ON ctv_directory (telegram_user_id);",
    "CREATE INDEX IF NOT EXISTS idx_ctv_directory_phone ON ctv_directory (phone);",
    "CREATE INDEX IF NOT EXISTS idx_form_tags_tag ON form_tags (tag);",
    "CREATE INDEX IF NOT EXISTS idx_ctv_quotas_code ON ctv_quotas (ctv_code, active);",
)

# Process-local caches: code -> directory row, (ctv_code, form_id) -> link row.
_CTV_DIRECTORY: Dict[str, Dict[str, Any]] = {}
_CTV_FORM_LINKS: Dict[Tuple[str, str], Dict[str, Any]] = {}


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


def get_ctv_codes(active_only: bool = True) -> List[str]:
    """Codes from the loaded directory, falling back to the static registry."""
    if _CTV_DIRECTORY:
        return [
            code
            for code, row in _CTV_DIRECTORY.items()
            if not active_only or row.get("is_active", True)
        ]
    return list(CTV_CODES)


def set_ctv_directory_cache(rows: Iterable[Dict[str, Any]]) -> None:
    """Replace the in-memory directory cache (also used by tests)."""
    _CTV_DIRECTORY.clear()
    for row in rows:
        code = normalize_ctv_code(str(row.get("code") or ""))
        if not code:
            continue
        _CTV_DIRECTORY[code] = {
            "code": code,
            "display_name": row.get("display_name"),
            "phone": row.get("phone"),
            "telegram_user_id": row.get("telegram_user_id"),
            "is_active": bool(row.get("is_active", True)),
            "notes": row.get("notes"),
        }


def clear_ctv_directory_cache() -> None:
    _CTV_DIRECTORY.clear()
    _CTV_FORM_LINKS.clear()


def get_ctv_directory() -> Dict[str, Dict[str, Any]]:
    return _CTV_DIRECTORY


def get_ctv_entry(code: str) -> Optional[Dict[str, Any]]:
    return _CTV_DIRECTORY.get(normalize_ctv_code(code))


def is_known_ctv_code(code: str) -> bool:
    normalized = normalize_ctv_code(code)
    if not normalized:
        return False
    if _CTV_DIRECTORY:
        row = _CTV_DIRECTORY.get(normalized)
        return bool(row and row.get("is_active", True))
    return normalized in CTV_CODE_MAP


def resolve_ctv_from_slug(slug: str) -> Optional[str]:
    """Resolves a CTV code from a deterministic slug."""
    for code in get_ctv_codes(active_only=False):
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
    if _CTV_DIRECTORY:
        if code in _CTV_DIRECTORY:
            return code
        return code or None
    if code in CTV_CODE_MAP:
        return CTV_CODE_MAP[code]
    slug_resolved = resolve_ctv_from_slug(ref.strip())
    if slug_resolved:
        return slug_resolved
    return code or None


def get_ctv_options_for_form(schema: FormSchema) -> List[str]:
    """Returns active directory codes; falls back to form/static options when empty."""
    directory_codes = get_ctv_codes(active_only=True)
    if _CTV_DIRECTORY:
        return directory_codes
    for q in schema.questions:
        if q.id in ("ctv_code", "collaborator_id") and q.options:
            return [normalize_ctv_code(str(opt)) for opt in q.options]
    return directory_codes


def set_ctv_form_links_cache(rows: Iterable[Dict[str, Any]]) -> None:
    _CTV_FORM_LINKS.clear()
    for row in rows:
        code = normalize_ctv_code(str(row.get("ctv_code") or ""))
        form_id = str(row.get("form_id") or "").strip()
        if not code or not form_id:
            continue
        _CTV_FORM_LINKS[(code, form_id)] = {
            "ctv_code": code,
            "form_id": form_id,
            "url": row.get("url"),
            "qr": row.get("qr"),
        }


def get_ctv_form_link(ctv_code: str, form_id: str) -> Optional[Dict[str, Any]]:
    return _CTV_FORM_LINKS.get((normalize_ctv_code(ctv_code), str(form_id).strip()))


def build_ctv_form_url(
    ctv_code: str,
    form_id: str,
    base_url: str = "https://survey.trustbridgevn.com",
) -> str:
    """Preferred per-CTV form URL from ctv_form_links, else the canonical ref link."""
    code = normalize_ctv_code(ctv_code)
    link = get_ctv_form_link(code, form_id)
    if link and link.get("url"):
        return str(link["url"])
    return f"{base_url}/forms/{form_id}" + (f"?ref={code}" if code else "")


async def refresh_ctv_directory_cache(db_pool=None) -> Dict[str, Dict[str, Any]]:
    """Reloads directory + form links into process-local caches."""
    if db_pool is None:
        try:
            from core.state import get_db_pool

            db_pool = get_db_pool()
        except Exception:
            db_pool = None
    if not db_pool:
        return _CTV_DIRECTORY

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT code, display_name, phone, telegram_user_id, is_active, notes "
                    "FROM ctv_directory ORDER BY code;"
                )
                rows = await cur.fetchall()
                set_ctv_directory_cache(
                    {
                        "code": r[0],
                        "display_name": r[1],
                        "phone": r[2],
                        "telegram_user_id": r[3],
                        "is_active": r[4],
                        "notes": r[5],
                    }
                    for r in rows
                )
                try:
                    await cur.execute(
                        "SELECT ctv_code, form_id, url, qr FROM ctv_form_links;"
                    )
                    link_rows = await cur.fetchall()
                    set_ctv_form_links_cache(
                        {"ctv_code": r[0], "form_id": r[1], "url": r[2], "qr": r[3]}
                        for r in link_rows
                    )
                except Exception as e:
                    logger.debug(f"CTV form link cache load skipped: {e}")
    except Exception as e:
        logger.warning(f"CTV directory cache refresh failed: {e}")
    return _CTV_DIRECTORY


async def seed_ctv_directory(cur) -> int:
    """Idempotently seeds ctv_directory from static codes + authorized_users.

    Returns the number of static codes ensured.
    """
    for code in CTV_CODES:
        await cur.execute(
            """
            INSERT INTO ctv_directory (code, display_name, notes)
            VALUES (%s, %s, 'seed:static')
            ON CONFLICT (code) DO NOTHING;
            """,
            (code, STATIC_CTV_NAMES.get(code) or code),
        )

    await cur.execute(
        "SELECT telegram_user_id, username, full_name, driver_phone, role "
        "FROM authorized_users;"
    )
    rows = await cur.fetchall()
    for tg_id, username, full_name, phone, role in rows:
        raw_ref = str(username or "").strip()
        if not raw_ref:
            continue
        code = normalize_ctv_code(raw_ref)
        if not code:
            continue
        looks_like_ctv = (
            code in CTV_CODE_MAP
            or str(role or "").upper() == "COLLABORATOR"
            or re.match(r"^CTV[_-]?", raw_ref, flags=re.IGNORECASE) is not None
        )
        if not looks_like_ctv:
            continue
        await cur.execute(
            """
            INSERT INTO ctv_directory (code, display_name, phone, telegram_user_id, notes)
            VALUES (%s, %s, %s, %s, 'seed:authorized_users')
            ON CONFLICT (code) DO UPDATE SET
                display_name = COALESCE(EXCLUDED.display_name, ctv_directory.display_name),
                phone = COALESCE(EXCLUDED.phone, ctv_directory.phone),
                telegram_user_id = COALESCE(EXCLUDED.telegram_user_id, ctv_directory.telegram_user_id),
                updated_at = NOW();
            """,
            (code, full_name or code, phone, tg_id),
        )
    return len(CTV_CODES)


async def ensure_ctv_platform_schema(db_pool=None) -> bool:
    """Creates the CTV platform tables (idempotent), seeds, and refreshes caches."""
    if db_pool is None:
        try:
            from core.state import get_db_pool

            db_pool = get_db_pool()
        except Exception:
            db_pool = None
    if not db_pool:
        logger.info("CTV platform schema ensure skipped (no DB pool); using static fallback.")
        return False
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                for statement in CTV_TABLE_STATEMENTS:
                    await cur.execute(statement)
                await seed_ctv_directory(cur)
            await conn.commit()
    except Exception as e:
        logger.warning(f"CTV platform schema ensure failed: {e}")
        return False
    await refresh_ctv_directory_cache(db_pool)
    logger.info(
        f"CTV platform ready: {len(_CTV_DIRECTORY)} directory rows, "
        f"{len(_CTV_FORM_LINKS)} form links cached."
    )
    return True


def format_ctv_options_html(ctv_options: Iterable[str]) -> str:
    """Renders <option> tags for CTV selectors (kept for callers that need HTML)."""
    options_html = '<option value="">-- Vui lòng chọn CTV phụ trách --</option>'
    seen = set()
    for opt in ctv_options:
        code = normalize_ctv_code(str(opt))
        if not code or code in seen:
            continue
        seen.add(code)
        options_html += f'<option value="{html.escape(code)}">{html.escape(code)}</option>'
    return options_html
