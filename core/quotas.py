# -*- coding: utf-8 -*-
"""
CTV quota storage & reporting.

Quotas are stored in `ctv_quotas` (scope_type: form | tag | global) and usage is
computed from `survey_submissions` over a rolling window of `period_days`. Tag
scopes match forms linked to the tag in `form_tags`. Nothing here blocks a
submission yet; this is storage + querying only, so enforcement can be added
later without another schema change.
"""

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.ctv import (
    get_ctv_directory,
    get_ctv_entry,
    is_known_ctv_code,
    normalize_ctv_code,
)

logger = logging.getLogger("core.quotas")

SCOPE_TYPES = ("form", "tag", "global")
SCOPE_LABELS = {"form": "Biểu mẫu", "tag": "Nhãn", "global": "Tổng"}


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _normalize_tag(value: Any) -> str:
    return str(value or "").strip().lower()


def _tags_map(form_tags: Any) -> Dict[str, Set[str]]:
    """Accepts {form_id: [tags]}, [(form_id, tag)], or {form_id: set(tags)}."""
    if not form_tags:
        return {}
    if isinstance(form_tags, dict):
        return {
            str(form_id): {_normalize_tag(t) for t in tags or []}
            for form_id, tags in form_tags.items()
        }
    result: Dict[str, Set[str]] = {}
    for form_id, tag in form_tags:
        result.setdefault(str(form_id), set()).add(_normalize_tag(tag))
    return result


def quota_window_start(period_days: Optional[int], now: Optional[datetime] = None) -> Optional[datetime]:
    if period_days is None or int(period_days) <= 0:
        return None
    current = _as_datetime(now) or datetime.now(timezone.utc)
    return current - timedelta(days=int(period_days))


def submission_matches_quota(
    quota: Dict[str, Any],
    submission: Dict[str, Any],
    form_tags: Optional[Dict[str, Set[str]]] = None,
) -> bool:
    scope_type = str(quota.get("scope_type") or "").strip().lower()
    scope_value = str(quota.get("scope_value") or "").strip()
    form_id = str(submission.get("form_id") or "")
    if scope_type == "form":
        return form_id == scope_value
    if scope_type == "tag":
        tags = (form_tags or {}).get(form_id) or set()
        return _normalize_tag(scope_value) in tags
    if scope_type == "global":
        return True
    return False


def evaluate_quota(
    quota: Dict[str, Any],
    submissions: Iterable[Dict[str, Any]],
    form_tags: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Counts matching submissions inside the rolling window and derives remaining.

    `submissions` only needs `form_id` and `created_at`; the caller is expected to
    pass rows already filtered to the CTV.
    """
    current = _as_datetime(now) or datetime.now(timezone.utc)
    period_days = quota.get("period_days")
    try:
        period_days = int(period_days) if period_days is not None else None
    except (TypeError, ValueError):
        period_days = None
    window_start = quota_window_start(period_days, current)
    tags_map = _tags_map(form_tags)

    used = 0
    oldest: Optional[datetime] = None
    for submission in submissions:
        created_at = _as_datetime(submission.get("created_at"))
        if window_start is not None:
            if created_at is None or created_at < window_start:
                continue
        if not submission_matches_quota(quota, submission, tags_map):
            continue
        used += 1
        if created_at is not None and (oldest is None or created_at < oldest):
            oldest = created_at

    max_submissions = quota.get("max_submissions")
    try:
        max_submissions = int(max_submissions) if max_submissions is not None else None
    except (TypeError, ValueError):
        max_submissions = None

    remaining = None
    if max_submissions is not None:
        remaining = max_submissions - used

    reset_at = None
    if period_days and period_days > 0 and oldest is not None:
        reset_at = oldest + timedelta(days=period_days)

    return {
        "quota_id": quota.get("id"),
        "scope_type": str(quota.get("scope_type") or "").strip().lower(),
        "scope_value": str(quota.get("scope_value") or "").strip(),
        "max_submissions": max_submissions,
        "period_days": period_days,
        "used": used,
        "remaining": remaining,
        "window_start": window_start,
        "reset_at": reset_at,
    }


def format_quota_evaluation(evaluation: Dict[str, Any]) -> str:
    scope_type = evaluation.get("scope_type") or "global"
    scope_label = SCOPE_LABELS.get(scope_type, scope_type)
    scope_value = evaluation.get("scope_value") or ""
    scope_display = (
        f" <code>{html.escape(scope_value)}</code>" if scope_value else ""
    )
    used = evaluation.get("used", 0)
    maximum = evaluation.get("max_submissions")
    if maximum is None:
        cap = "không giới hạn"
    else:
        remaining = evaluation.get("remaining")
        remaining_text = remaining if remaining is not None else 0
        cap = f"{used}/{maximum} (còn {remaining_text})"
    if maximum is None:
        usage = f"{used} lượt"
    else:
        usage = cap
    reset_at = evaluation.get("reset_at")
    if reset_at:
        reset_text = reset_at.strftime("%d/%m/%Y")
    elif evaluation.get("period_days"):
        reset_text = "cuốn chiếu"
    else:
        reset_text = "không đặt"
    return (
        f"• <b>{scope_label}</b>{scope_display}: {usage} "
        f"- reset: <i>{reset_text}</i>"
    )


def format_quota_report(
    ctv_code: str,
    display_name: Optional[str],
    evaluations: List[Dict[str, Any]],
) -> str:
    code = normalize_ctv_code(ctv_code)
    label = html.escape(display_name or code)
    header = f"📊 <b>Hạn mức CTV <code>{html.escape(code)}</code></b>"
    if display_name and display_name != code:
        header += f" - {label}"
    if not evaluations:
        return header + "\n\n<i>Chưa cấu hình hạn mức cho CTV này.</i>"
    lines = [header, ""]
    for evaluation in evaluations:
        lines.append(format_quota_evaluation(evaluation))
    lines.append("")
    lines.append("<i>Hạn mức hiện chỉ để tra cứu, chưa chặn nộp phiếu.</i>")
    return "\n".join(lines)


async def fetch_active_quotas(db_pool, ctv_code: str) -> List[Dict[str, Any]]:
    if not db_pool:
        return []
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, ctv_code, scope_type, scope_value, max_submissions, "
                    "period_days, active FROM ctv_quotas "
                    "WHERE UPPER(ctv_code) = %s AND active = TRUE ORDER BY id;",
                    (normalize_ctv_code(ctv_code),),
                )
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"Quota query failed for {ctv_code}: {e}")
        return []
    return [
        {
            "id": row[0],
            "ctv_code": row[1],
            "scope_type": row[2],
            "scope_value": row[3],
            "max_submissions": row[4],
            "period_days": row[5],
            "active": row[6],
        }
        for row in rows
    ]


async def fetch_ctv_submission_rows(
    db_pool, ctv_code: str, since: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    if not db_pool:
        return []
    query = (
        "SELECT form_id, created_at FROM survey_submissions "
        "WHERE UPPER(ctv_code) = %s"
    )
    params: List[Any] = [normalize_ctv_code(ctv_code)]
    if since is not None:
        query += " AND created_at >= %s"
        params.append(since)
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, tuple(params))
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"Quota usage query failed for {ctv_code}: {e}")
        return []
    return [{"form_id": row[0], "created_at": row[1]} for row in rows]


async def fetch_form_tags_map(
    db_pool, form_ids: Optional[Iterable[str]] = None
) -> Dict[str, Set[str]]:
    if not db_pool:
        return {}
    query = "SELECT form_id, tag FROM form_tags"
    params: Tuple[Any, ...] = ()
    if form_ids is not None:
        ids = sorted({str(f) for f in form_ids if f})
        if not ids:
            return {}
        query += " WHERE form_id = ANY(%s)"
        params = (ids,)
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"form_tags query failed: {e}")
        return {}
    result: Dict[str, Set[str]] = {}
    for form_id, tag in rows:
        result.setdefault(str(form_id), set()).add(_normalize_tag(tag))
    return result


async def get_ctv_quota_evaluations(
    db_pool, ctv_code: str, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    quotas = await fetch_active_quotas(db_pool, ctv_code)
    if not quotas:
        return []
    current = _as_datetime(now) or datetime.now(timezone.utc)
    periods = [
        int(q["period_days"])
        for q in quotas
        if q.get("period_days") is not None
    ]
    since = quota_window_start(max(periods), current) if periods else None
    submissions = await fetch_ctv_submission_rows(db_pool, ctv_code, since)
    form_ids = [
        str(q.get("scope_value"))
        for q in quotas
        if str(q.get("scope_type") or "").lower() == "form" and q.get("scope_value")
    ]
    has_tag_scope = any(
        str(q.get("scope_type") or "").lower() == "tag" for q in quotas
    )
    tags_map = await fetch_form_tags_map(db_pool, None if has_tag_scope else (form_ids or None))
    return [evaluate_quota(q, submissions, tags_map, current) for q in quotas]


async def get_ctv_display_name(db_pool, ctv_code: str) -> Optional[str]:
    entry = get_ctv_entry(ctv_code)
    if entry and entry.get("display_name"):
        return str(entry["display_name"])
    if not db_pool:
        return None
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT display_name FROM ctv_directory WHERE UPPER(code) = %s LIMIT 1;",
                    (normalize_ctv_code(ctv_code),),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    return str(row[0])
    except Exception as e:
        logger.debug(f"CTV display name lookup failed for {ctv_code}: {e}")
    return None


async def build_ctv_quota_report(db_pool, ctv_code: str) -> str:
    code = normalize_ctv_code(ctv_code)
    display_name = await get_ctv_display_name(db_pool, code)
    evaluations = await get_ctv_quota_evaluations(db_pool, code)
    return format_quota_report(code, display_name, evaluations)


async def resolve_ctv_code_from_ref(db_pool, ref: str) -> Optional[str]:
    """Resolves a user-supplied CTV code/ref against ctv_directory."""
    code = normalize_ctv_code(ref)
    if not code:
        return None
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT code FROM ctv_directory WHERE UPPER(code) = %s LIMIT 1;",
                        (code,),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return normalize_ctv_code(str(row[0]))
        except Exception as e:
            logger.debug(f"CTV ref lookup failed for {ref}: {e}")
    if get_ctv_entry(code) or is_known_ctv_code(code):
        return code
    return None


async def resolve_ctv_code_for_telegram(db_pool, telegram_user_id: int) -> Optional[str]:
    """Resolves a Telegram account to its CTV code (directory, phone, username)."""
    if not telegram_user_id:
        return None
    tg_id = int(telegram_user_id)
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT code FROM ctv_directory WHERE telegram_user_id = %s "
                        "AND is_active = TRUE LIMIT 1;",
                        (tg_id,),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return normalize_ctv_code(str(row[0]))

                    await cur.execute(
                        "SELECT driver_phone, username FROM authorized_users "
                        "WHERE telegram_user_id = %s LIMIT 1;",
                        (tg_id,),
                    )
                    row = await cur.fetchone()
                    phone = str(row[0]).strip() if row and row[0] else None
                    username = str(row[1]).strip() if row and row[1] else None

                    if phone:
                        await cur.execute(
                            "SELECT code FROM ctv_directory WHERE is_active = TRUE "
                            "AND REPLACE(REPLACE(phone, ' ', ''), '-', '') = "
                            "REPLACE(REPLACE(%s, ' ', ''), '-', '') LIMIT 1;",
                            (phone,),
                        )
                        row = await cur.fetchone()
                        if row and row[0]:
                            return normalize_ctv_code(str(row[0]))
                    if username:
                        code = normalize_ctv_code(username)
                        if code:
                            return code
        except Exception as e:
            logger.debug(f"CTV telegram resolution failed for {tg_id}: {e}")

    # In-memory fallback (cache may be loaded even if this DB call failed).
    for code, entry in get_ctv_directory().items():
        if entry.get("telegram_user_id") and int(entry["telegram_user_id"]) == tg_id:
            return code
    return None
