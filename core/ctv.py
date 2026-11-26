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
from core.ctv_onboarding import CTV_ONBOARDING_STATEMENTS

logger = logging.getLogger("core.ctv")

CTV_CODES = (
    "TUAN_LM_HCM_001",
    "DUNG_NV_HCM_002",
    "KHUYEN_TL_HCM_003",
    "VY_PV_HCM_004",
)

# Canonical code -> canonical code. Kept as a map for callers that iterate it.
CTV_CODE_MAP = {code: code for code in CTV_CODES}

# Display names use the operators' preferred mixed/diacritic spelling.
STATIC_CTV_NAMES = {
    "TUAN_LM_HCM_001": "Tuấn_LM_HCM_001",
    "DUNG_NV_HCM_002": "Dũng_NV_HCM_002",
    "KHUYEN_TL_HCM_003": "Khuyên_TL_HCM_003",
    "VY_PV_HCM_004": "Vỹ_PV_HCM_004",
}

# Only these previous static labels are migrated to STATIC_CTV_NAMES on startup.
PREVIOUS_STATIC_CTV_NAMES = {
    "TUAN_LM_HCM_001": "Tuấn LM (HCM)",
    "DUNG_NV_HCM_002": "Dũng NV (HCM)",
    "KHUYEN_TL_HCM_003": "Khuyên TL (HCM)",
    "VY_PV_HCM_004": "Vy PV (HCM)",
}

# Legacy submission-derived codes that must be removed idempotently.
# Any other admin-added directory code is left untouched.
LEGACY_SUBMISSION_CODES = (
    "CTV001_Dũng_HN",
    "CTV002_Tuấn_HN",
    "CTV003_Ngân_SG",
    "CTV004_Vỹ_SG",
)

CTV_TABLE_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ctv_directory (
        code TEXT PRIMARY KEY,
        display_name TEXT,
        phone TEXT,
        telegram_user_id BIGINT,
        is_active BOOLEAN DEFAULT TRUE,
        auth_revision UUID NOT NULL DEFAULT gen_random_uuid(),
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "ALTER TABLE ctv_directory ADD COLUMN IF NOT EXISTS auth_revision UUID NOT NULL DEFAULT gen_random_uuid();",
    """
    CREATE OR REPLACE FUNCTION rotate_ctv_auth_revision() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF ROW(NEW.code, NEW.phone, NEW.telegram_user_id, NEW.is_active)
            IS DISTINCT FROM ROW(OLD.code, OLD.phone, OLD.telegram_user_id, OLD.is_active) THEN
            NEW.auth_revision := gen_random_uuid();
        ELSE
            NEW.auth_revision := OLD.auth_revision;
        END IF;
        RETURN NEW;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE TRIGGER ctv_directory_auth_revision
    BEFORE UPDATE ON ctv_directory
    FOR EACH ROW EXECUTE FUNCTION rotate_ctv_auth_revision();
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
    CREATE TABLE IF NOT EXISTS ctv_form_assignments (
        ctv_code TEXT NOT NULL REFERENCES ctv_directory(code) ON DELETE CASCADE,
        form_id TEXT NOT NULL,
        assigned_by TEXT,
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (ctv_code, form_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_ctv_form_assignments_form ON ctv_form_assignments (form_id);",
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
) + CTV_ONBOARDING_STATEMENTS

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
        code = str(row.get("code") or "").strip()
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


def _find_ctv_code(value: str) -> Optional[str]:
    """Finds the stored directory code for a case/diacritic-insensitive input."""
    raw = str(value or "").strip()
    if not raw or not _CTV_DIRECTORY:
        return None
    if raw in _CTV_DIRECTORY:
        return raw
    if raw.upper() in {code.upper() for code in _CTV_DIRECTORY}:
        for code in _CTV_DIRECTORY:
            if code.upper() == raw.upper():
                return code
    target = normalize_ctv_code(raw)
    for code in _CTV_DIRECTORY:
        if normalize_ctv_code(code) == target:
            return code
    return None


def get_ctv_entry(code: str) -> Optional[Dict[str, Any]]:
    stored = _find_ctv_code(code)
    return _CTV_DIRECTORY.get(stored) if stored else None


def is_known_ctv_code(code: str) -> bool:
    normalized = normalize_ctv_code(code)
    if not normalized:
        return False
    if _CTV_DIRECTORY:
        row = get_ctv_entry(code)
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
    """Resolves a ref/slug/CTV name to the stored directory code."""
    if not ref:
        return None
    code = normalize_ctv_code(ref)
    if _CTV_DIRECTORY:
        stored = _find_ctv_code(ref)
        if stored:
            return stored
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
    entry = get_ctv_entry(ctv_code)
    code = entry["code"] if entry else normalize_ctv_code(ctv_code)
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


def _clean_phone(value: Any) -> Optional[str]:
    raw = re.sub(r"[^\d+]", "", str(value or "").strip())
    if raw.startswith("+84"):
        raw = "0" + raw[3:]
    elif raw.startswith("84") and len(raw) == 11:
        raw = "0" + raw[2:]
    return raw if len(raw) >= 8 else None


async def cleanup_legacy_submission_ctvs(cur) -> Dict[str, int]:
    """Deletes the four known submission-derived CTVs and their links/quotas.

    Only the exact codes in LEGACY_SUBMISSION_CODES are touched; admin-added
    directory rows survive.
    """
    legacy = list(LEGACY_SUBMISSION_CODES)
    deleted = {"directory": 0, "links": 0, "quotas": 0}

    await cur.execute(
        "DELETE FROM ctv_form_links WHERE ctv_code = ANY(%s);", (legacy,)
    )
    deleted["links"] = int(getattr(cur, "rowcount", 0) or 0)

    await cur.execute(
        "DELETE FROM ctv_quotas WHERE ctv_code = ANY(%s);", (legacy,)
    )
    deleted["quotas"] = int(getattr(cur, "rowcount", 0) or 0)

    await cur.execute(
        "DELETE FROM ctv_directory WHERE code = ANY(%s);", (legacy,)
    )
    deleted["directory"] = int(getattr(cur, "rowcount", 0) or 0)
    return deleted


async def seed_ctv_directory(cur) -> Dict[str, Any]:
    """Idempotently enforces the four canonical CTV rows + removes legacy codes.

    It no longer seeds from survey_submissions or dev storage. Directory display
    names are migrated from the old static labels only; admin-edited names stay.
    """
    stats: Dict[str, Any] = {
        "static": 0,
        "authorized": 0,
        "phone_sources": {},
        "legacy_deleted": {},
    }

    stats["legacy_deleted"] = await cleanup_legacy_submission_ctvs(cur)

    for code in CTV_CODES:
        new_name = STATIC_CTV_NAMES.get(code) or code
        old_name = PREVIOUS_STATIC_CTV_NAMES.get(code)
        await cur.execute(
            """
            INSERT INTO ctv_directory (code, display_name, notes)
            VALUES (%s, %s, 'seed:static')
            ON CONFLICT (code) DO UPDATE SET
                display_name = CASE
                    WHEN ctv_directory.display_name IS NULL
                        OR ctv_directory.display_name = ctv_directory.code
                        OR ctv_directory.display_name = %s
                    THEN EXCLUDED.display_name
                    ELSE ctv_directory.display_name
                END,
                updated_at = NOW();
            """,
            (code, new_name, old_name),
        )
        stats["static"] += 1

    # Repair legacy/manual data: a canonical CTV code may only live on an
    # explicit COLLABORATOR row. Driver rows keep their real Telegram username
    # (or NULL) and are never given a CTV identifier.
    await cur.execute(
        "UPDATE authorized_users SET username = NULL "
        "WHERE username IS NOT NULL "
        "AND UPPER(TRIM(username)) = ANY(%s) "
        "AND NOT ('COLLABORATOR' = ANY(roles));",
        (list(CTV_CODES),),
    )

    # Only explicit CTV bindings may populate the directory. A driver phone is
    # never a CTV target: question-source verdicts resolve the phone directly.
    await cur.execute(
        "SELECT telegram_user_id, username, full_name, driver_phone, roles "
        "FROM authorized_users;"
    )
    rows = await cur.fetchall()
    for tg_id, username, full_name, phone, roles in rows:
        raw_ref = str(username or "").strip()
        if not raw_ref or "COLLABORATOR" not in (roles or ()):
            continue
        code = normalize_ctv_code(raw_ref)
        if code not in CTV_CODE_MAP:
            continue
        clean_phone = _clean_phone(phone)
        await cur.execute(
            """
            UPDATE ctv_directory SET
                phone = COALESCE(ctv_directory.phone, %s),
                telegram_user_id = COALESCE(ctv_directory.telegram_user_id, %s),
                updated_at = NOW()
            WHERE code = %s;
            """,
            (clean_phone, tg_id, code),
        )
        stats["authorized"] += 1
        if clean_phone:
            stats["phone_sources"]["authorized_users"] = (
                stats["phone_sources"].get("authorized_users", 0) + 1
            )

    logger.info(
        "CTV directory seed: static=%s authorized=%s legacy_deleted=%s phone_sources=%s",
        stats["static"],
        stats["authorized"],
        stats["legacy_deleted"],
        stats["phone_sources"],
    )
    return stats


async def update_ctv_directory_entry(
    db_pool,
    code: str,
    display_name: Optional[str] = None,
    phone: Optional[str] = None,
    is_active: bool = True,
    notes: Optional[str] = None,
) -> bool:
    """Updates one directory row and refreshes the process cache.

    `telegram_user_id` is populated internally when a CTV binds the Telegram bot.
    `notes` is only changed when supplied (empty string clears it).
    """
    normalized = normalize_ctv_code(code)
    if not db_pool or not normalized:
        return False
    entry = get_ctv_entry(code)
    stored_code = entry["code"] if entry else str(code).strip()
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                if notes is None:
                    await cur.execute(
                        """
                        UPDATE ctv_directory SET
                            display_name = %s,
                            phone = %s,
                            is_active = %s,
                            updated_at = NOW()
                        WHERE code = %s;
                        """,
                        (
                            str(display_name).strip() or None if display_name is not None else None,
                            _clean_phone(phone) if phone else None,
                            bool(is_active),
                            stored_code,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE ctv_directory SET
                            display_name = %s,
                            phone = %s,
                            notes = %s,
                            is_active = %s,
                            updated_at = NOW()
                        WHERE code = %s;
                        """,
                        (
                            str(display_name).strip() or None if display_name is not None else None,
                            _clean_phone(phone) if phone else None,
                            str(notes).strip() or None,
                            bool(is_active),
                            stored_code,
                        ),
                    )
                updated = int(getattr(cur, "rowcount", 1) or 0) > 0
            await conn.commit()
    except Exception as e:
        logger.warning(f"CTV directory update failed for {code}: {e}")
        return False
    if updated:
        await refresh_ctv_directory_cache(db_pool)
    return updated


def is_test_ctv(code: Optional[str], display_name: Optional[str] = None) -> bool:
    """Returns True when either code or display_name marks this CTV as a test account."""
    return "TEST" in str(code or "").upper() or "TEST" in str(display_name or "").upper()


def _is_open_public_form(schema: Any) -> bool:
    """True when a registered form is public and not closed by response_limit."""
    form_settings = getattr(schema, "settings", None)
    if form_settings is None:
        return True
    if getattr(form_settings, "access_control", "public") != "public":
        return False
    limit = getattr(form_settings, "response_limit", None)
    if limit is not None:
        if limit < 0:
            return False
        if limit > 0 and int(getattr(form_settings, "current_responses", 0) or 0) >= limit:
            return False
    return True


async def assign_default_forms_for_new_ctv(
    db_pool,
    ctv_code: str,
    *,
    display_name: Optional[str] = None,
    is_active: bool = True,
    assigned_by: str = "default_new_ctv",
) -> List[str]:
    """Auto-assigns a brand-new active non-test CTV to all currently open public forms."""
    normalized = normalize_ctv_code(ctv_code)
    if not db_pool or not normalized or not is_active or is_test_ctv(normalized, display_name):
        return []
    open_form_ids = sorted(
        fid for fid, schema in FORM_REGISTRY.items() if _is_open_public_form(schema)
    )
    if not open_form_ids:
        return []
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                for fid in open_form_ids:
                    await cur.execute(
                        "INSERT INTO ctv_form_assignments (ctv_code, form_id, assigned_by) "
                        "VALUES (%s, %s, %s) ON CONFLICT (ctv_code, form_id) DO NOTHING;",
                        (normalized, fid, assigned_by),
                    )
            await conn.commit()
    except Exception as e:
        logger.warning(f"Default form assignment failed for new CTV {normalized}: {e}")
        return []
    return open_form_ids


async def assign_default_ctvs_for_new_form(
    db_pool,
    form_id: str,
    *,
    assigned_by: str = "default_new_form",
) -> List[str]:
    """Auto-assigns all active non-test CTVs to a brand-new form."""
    fid = str(form_id or "").strip()
    if not db_pool or not fid:
        return []
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO ctv_form_assignments (ctv_code, form_id, assigned_by)
                    SELECT code, %s, %s
                    FROM ctv_directory
                    WHERE is_active = TRUE
                      AND UPPER(code) NOT LIKE '%%TEST%%'
                      AND UPPER(COALESCE(display_name, '')) NOT LIKE '%%TEST%%'
                    ON CONFLICT (ctv_code, form_id) DO NOTHING
                    RETURNING ctv_code;
                    """,
                    (fid, assigned_by),
                )
                rows = await cur.fetchall()
            await conn.commit()
        return [str(row[0]) for row in rows if row and row[0]]
    except Exception as e:
        logger.warning(f"Default CTV assignment failed for new form {fid}: {e}")
        return []


async def create_ctv_directory_entry(
    db_pool,
    code: str,
    display_name: Optional[str] = None,
    phone: Optional[str] = None,
    telegram_user_id: Optional[int] = None,
    notes: Optional[str] = None,
    is_active: bool = True,
) -> bool:
    """Creates one CTV directory row. Returns False when the code exists."""
    normalized = normalize_ctv_code(code)
    if not db_pool or not normalized:
        return False
    cleaned_display_name = str(display_name).strip() or None if display_name is not None else None
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO ctv_directory
                        (code, display_name, phone, telegram_user_id, is_active, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO NOTHING
                    RETURNING code;
                    """,
                    (
                        normalized,
                        cleaned_display_name,
                        _clean_phone(phone) if phone else None,
                        int(telegram_user_id) if telegram_user_id not in (None, "") else None,
                        bool(is_active),
                        str(notes).strip() or None if notes is not None else None,
                    ),
                )
                created = await cur.fetchone() is not None
            await conn.commit()
    except Exception as e:
        logger.warning(f"CTV directory create failed for {code}: {e}")
        return False
    if created:
        if bool(is_active) and not is_test_ctv(normalized, cleaned_display_name):
            await assign_default_forms_for_new_ctv(
                db_pool,
                normalized,
                display_name=cleaned_display_name,
                is_active=True,
                assigned_by="default_new_ctv",
            )
        await refresh_ctv_directory_cache(db_pool)
    return created


async def delete_ctv_directory_entry(db_pool, code: str) -> bool:
    """Removes a CTV and its form links/quotas. Historical submissions stay intact."""
    normalized = normalize_ctv_code(code)
    if not db_pool or not normalized:
        return False
    entry = get_ctv_entry(code)
    stored_code = entry["code"] if entry else str(code).strip()
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM ctv_quotas WHERE ctv_code = %s", (stored_code,))
                await cur.execute("DELETE FROM ctv_form_links WHERE ctv_code = %s", (stored_code,))
                await cur.execute("DELETE FROM ctv_form_assignments WHERE ctv_code = %s", (stored_code,))
                await cur.execute("DELETE FROM ctv_directory WHERE code = %s RETURNING code", (stored_code,))
                deleted = await cur.fetchone() is not None
            await conn.commit()
    except Exception as e:
        logger.warning(f"CTV directory delete failed for {code}: {e}")
        return False
    if deleted:
        await refresh_ctv_directory_cache(db_pool)
    return deleted


async def get_ctv_assigned_form_ids(db_pool, ctv_code: str, *, strict: bool = False) -> set[str]:
    """Returns form_ids explicitly assigned to an active CTV."""
    raw = str(ctv_code or "").strip()
    if not raw:
        return set()
    if not db_pool:
        if strict:
            raise RuntimeError("CTV assignment database unavailable")
        return set()
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT form_id FROM ctv_form_assignments WHERE UPPER(ctv_code) = UPPER(%s)",
                    (raw,),
                )
                return {str(row[0]) for row in await cur.fetchall() if row and row[0]}
    except Exception:
        if strict:
            raise
        logger.warning("CTV assignment lookup failed for %s", raw)
        return set()


async def get_all_ctv_assignments(db_pool) -> Dict[str, set[str]]:
    """Returns {ctv_code: {form_id, ...}} for admin directory display."""
    if not db_pool:
        return {}
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ctv_code, form_id FROM ctv_form_assignments ORDER BY ctv_code, form_id"
                )
                result: Dict[str, set[str]] = {}
                for ctv_code, form_id in await cur.fetchall():
                    if ctv_code and form_id:
                        result.setdefault(str(ctv_code), set()).add(str(form_id))
                return result
    except Exception as e:
        logger.warning("Failed to load all CTV assignments: %s", e)
        return {}


async def get_assigned_ctvs_for_form(db_pool, form_id: str) -> List[str]:
    """Returns active CTV codes assigned to a specific survey form."""
    fid = str(form_id or "").strip()
    if not db_pool or not fid:
        return []
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT a.ctv_code FROM ctv_form_assignments a "
                "JOIN ctv_directory d ON UPPER(d.code) = UPPER(a.ctv_code) "
                "WHERE a.form_id = %s AND d.is_active = TRUE ORDER BY a.ctv_code",
                (fid,),
            )
            return [str(row[0]) for row in await cur.fetchall() if row and row[0]]


async def is_ctv_assigned_to_form(db_pool, ctv_code: str, form_id: str) -> bool:
    """Live PostgreSQL authorization check: CTV must be active and assigned to form_id."""
    raw = str(ctv_code or "").strip()
    fid = str(form_id or "").strip()
    if not raw or not fid:
        return False
    if not db_pool:
        raise RuntimeError("CTV assignment database unavailable")
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM ctv_form_assignments a "
                "JOIN ctv_directory d ON UPPER(d.code) = UPPER(a.ctv_code) "
                "WHERE UPPER(a.ctv_code) = UPPER(%s) AND a.form_id = %s AND d.is_active = TRUE LIMIT 1",
                (raw, fid),
            )
            return await cur.fetchone() is not None


async def set_ctv_form_assignments(
    db_pool,
    ctv_code: str,
    form_ids: Iterable[str],
    assigned_by: Optional[str] = None,
) -> Optional[List[str]]:
    """Transactionally replaces the assigned survey set for a CTV."""
    raw = str(ctv_code or "").strip()
    if not db_pool or not raw:
        return None
    cleaned = sorted({str(fid).strip() for fid in form_ids if str(fid or "").strip()})
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT code FROM ctv_directory WHERE UPPER(code) = UPPER(%s) FOR UPDATE",
                (raw,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            stored_code = str(row[0])
            await cur.execute("DELETE FROM ctv_form_assignments WHERE ctv_code = %s", (stored_code,))
            for fid in cleaned:
                await cur.execute(
                    "INSERT INTO ctv_form_assignments (ctv_code, form_id, assigned_by) "
                    "VALUES (%s, %s, %s) ON CONFLICT (ctv_code, form_id) DO NOTHING",
                    (stored_code, fid, assigned_by),
                )
        await conn.commit()
    return cleaned


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
                stats = await seed_ctv_directory(cur)
            await conn.commit()
    except Exception as e:
        logger.warning(f"CTV platform schema ensure failed: {e}")
        return False
    await refresh_ctv_directory_cache(db_pool)
    logger.info(
        f"CTV platform ready: {len(_CTV_DIRECTORY)} directory rows, "
        f"{len(_CTV_FORM_LINKS)} form links cached; seed={stats}."
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
