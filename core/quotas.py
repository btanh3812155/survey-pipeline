# -*- coding: utf-8 -*-
"""
CTV quota storage & reporting.

Quotas are stored in `ctv_quotas` (scope_type: form | tag | global) and usage is
computed from `survey_submissions` over a rolling window of `period_days`. Tag
scopes match forms linked to the tag in `form_tags`. Quotas are goals for
progress tracking; nothing here blocks a submission.
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
        sub_status = submission.get("status")
        if sub_status is not None and str(sub_status).strip().upper() != "APPROVED":
            continue
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
        usage = f"{used} lượt"
    else:
        remaining = max(0, int(maximum) - int(used))
        if int(used) >= int(maximum):
            usage = f"đã đạt mục tiêu {used}/{maximum}"
        else:
            usage = f"đã đạt {used}/{maximum} (còn {remaining} để đạt mục tiêu)"
    reset_at = evaluation.get("reset_at")
    if reset_at:
        reset_text = reset_at.strftime("%d/%m/%Y")
    elif evaluation.get("period_days"):
        reset_text = "cuốn chiếu"
    else:
        reset_text = "không đặt"
    return (
        f"• <b>Mục tiêu {scope_label}</b>{scope_display}: {usage} "
        f"- reset: <i>{reset_text}</i>"
    )


def format_quota_report(
    ctv_code: str,
    display_name: Optional[str],
    evaluations: List[Dict[str, Any]],
) -> str:
    code = str(ctv_code or "").strip() or normalize_ctv_code(ctv_code)
    label = html.escape(display_name or code)
    header = f"🎯 <b>Mục tiêu CTV <code>{html.escape(code)}</code></b>"
    if display_name and display_name != code:
        header += f" - {label}"
    if not evaluations:
        return header + "\n\n<i>Chưa cấu hình mục tiêu cho CTV này.</i>"
    lines = [header, ""]
    for evaluation in evaluations:
        lines.append(format_quota_evaluation(evaluation))
    lines.append("")
    lines.append("<i>Mục tiêu chỉ để theo dõi tiến độ, không chặn nộp phiếu.</i>")
    return "\n".join(lines)


async def fetch_active_quotas(
    db_pool, ctv_code: str, *, strict: bool = False
) -> List[Dict[str, Any]]:
    if not db_pool:
        if strict:
            raise RuntimeError("Quota database unavailable")
        return []
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, ctv_code, scope_type, scope_value, max_submissions, "
                    "period_days, active FROM ctv_quotas "
                    "WHERE (UPPER(ctv_code) = UPPER(%s) OR UPPER(ctv_code) = UPPER(%s)) "
                    "AND active = TRUE ORDER BY id;",
                    (str(ctv_code).strip(), normalize_ctv_code(ctv_code)),
                )
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"Quota query failed for {ctv_code}: {e}")
        if strict:
            raise
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
    db_pool, ctv_code: str, since: Optional[datetime] = None, *, strict: bool = False
) -> List[Dict[str, Any]]:
    if not db_pool:
        if strict:
            raise RuntimeError("Quota database unavailable")
        return []
    query = (
        "SELECT form_id, created_at FROM survey_submissions "
        "WHERE (UPPER(ctv_code) = UPPER(%s) OR UPPER(ctv_code) = UPPER(%s)) "
        "AND status = 'APPROVED'"
    )
    params: List[Any] = [str(ctv_code).strip(), normalize_ctv_code(ctv_code)]
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
        if strict:
            raise
        return []
    return [{"form_id": row[0], "created_at": row[1]} for row in rows]


async def fetch_form_tags_map(
    db_pool, form_ids: Optional[Iterable[str]] = None, *, strict: bool = False
) -> Dict[str, Set[str]]:
    if not db_pool:
        if strict:
            raise RuntimeError("Quota database unavailable")
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
        if strict:
            raise
        return {}
    result: Dict[str, Set[str]] = {}
    for form_id, tag in rows:
        result.setdefault(str(form_id), set()).add(_normalize_tag(tag))
    return result


async def get_ctv_quota_evaluations(
    db_pool, ctv_code: str, now: Optional[datetime] = None, *, strict: bool = False
) -> List[Dict[str, Any]]:
    quotas = await fetch_active_quotas(db_pool, ctv_code, strict=strict)
    if not quotas:
        return []
    current = _as_datetime(now) or datetime.now(timezone.utc)
    periods = [
        int(q["period_days"])
        for q in quotas
        if q.get("period_days") is not None
    ]
    since = (
        quota_window_start(max(periods), current)
        if periods and all(q.get("period_days") and int(q["period_days"]) > 0 for q in quotas)
        else None
    )
    submissions = await fetch_ctv_submission_rows(db_pool, ctv_code, since, strict=strict)
    form_ids = [
        str(q.get("scope_value"))
        for q in quotas
        if str(q.get("scope_type") or "").lower() == "form" and q.get("scope_value")
    ]
    has_tag_scope = any(
        str(q.get("scope_type") or "").lower() == "tag" for q in quotas
    )
    tags_map = await fetch_form_tags_map(
        db_pool, None if has_tag_scope else (form_ids or None), strict=strict
    )
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
                    "SELECT display_name FROM ctv_directory "
                    "WHERE (UPPER(code) = UPPER(%s) OR UPPER(code) = UPPER(%s)) LIMIT 1;",
                    (str(ctv_code).strip(), normalize_ctv_code(ctv_code)),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    return str(row[0])
    except Exception as e:
        logger.debug(f"CTV display name lookup failed for {ctv_code}: {e}")
    return None


async def build_ctv_quota_report(db_pool, ctv_code: str) -> str:
    entry = get_ctv_entry(ctv_code)
    code = entry["code"] if entry else str(ctv_code or "").strip()
    display_name = await get_ctv_display_name(db_pool, code)
    evaluations = await get_ctv_quota_evaluations(db_pool, code)
    return format_quota_report(code, display_name, evaluations)


async def resolve_ctv_code_from_ref(db_pool, ref: str) -> Optional[str]:
    """Resolves a user-supplied CTV code/ref against ctv_directory."""
    code = normalize_ctv_code(ref)
    if not code:
        return None
    entry = get_ctv_entry(ref)
    if entry:
        return str(entry["code"])
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT code FROM ctv_directory "
                        "WHERE (UPPER(code) = UPPER(%s) OR UPPER(code) = UPPER(%s)) LIMIT 1;",
                        (str(ref).strip().lstrip("@"), code),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return str(row[0]).strip()
        except Exception as e:
            logger.debug(f"CTV ref lookup failed for {ref}: {e}")
    if is_known_ctv_code(code):
        return code
    return None


async def resolve_ctv_code_for_telegram(db_pool, telegram_user_id: int) -> Optional[str]:
    """Resolve CTV authority only from the live PostgreSQL directory binding."""
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
                        return str(row[0]).strip()

        except Exception as e:
            logger.debug("CTV telegram resolution failed for %s (%s)", tg_id, type(e).__name__)
    return None


def form_response_limit_status(
    schema: Any,
    approved_count: Optional[int] = None,
) -> Tuple[bool, int, Optional[int]]:
    """Evaluate campaign-wide response_limit against an APPROVED count.

    Convention:
      - response_limit is None or 0 -> unlimited (False)
      - response_limit < 0          -> closed (True)
      - response_limit > 0          -> capped when count >= response_limit
    """
    form_settings = getattr(schema, "settings", None) if schema else None
    raw_limit = getattr(form_settings, "response_limit", None) if form_settings else None
    if raw_limit is None:
        return False, 0, None
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return False, 0, None
    if limit == 0:
        return False, 0, 0
    if limit < 0:
        return True, 0, limit
    fallback = int(getattr(form_settings, "current_responses", 0) or 0)
    count = max(int(approved_count), fallback) if approved_count is not None else fallback
    return count >= limit, count, limit


async def count_approved_form_responses(
    db_pool,
    form_id: str,
    *,
    strict: bool = False,
) -> Optional[int]:
    """Return the live APPROVED submission count for one form, or None if unavailable."""
    if not db_pool:
        if strict:
            raise RuntimeError("Submission database unavailable")
        return None
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM survey_submissions "
                    "WHERE form_id = %s AND status = 'APPROVED';",
                    (str(form_id),),
                )
                row = await cur.fetchone()
    except Exception as e:
        logger.warning("Approved response count query failed for %s: %s", form_id, e)
        if strict:
            raise
        return None
    return int(row[0] or 0) if row else 0


async def count_approved_responses_by_form(
    db_pool,
    form_ids: Iterable[str],
    *,
    strict: bool = False,
) -> Dict[str, int]:
    """Return {form_id: approved_count} from survey_submissions for the given form_ids."""
    ids = sorted({str(fid) for fid in form_ids if fid})
    if not ids:
        return {}
    if not db_pool:
        if strict:
            raise RuntimeError("Submission database unavailable")
        return {}
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT form_id, COUNT(*) FROM survey_submissions "
                    "WHERE form_id = ANY(%s) AND status = 'APPROVED' "
                    "GROUP BY form_id;",
                    (ids,),
                )
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning("Approved response counts query failed: %s", e)
        if strict:
            raise
        return {}
    return {str(row[0]): int(row[1] or 0) for row in rows}


async def is_form_response_limit_reached(
    db_pool,
    form_id: str,
    schema: Any,
    *,
    strict: bool = False,
    approved_counts: Optional[Dict[str, int]] = None,
) -> Tuple[bool, int, Optional[int]]:
    """Check whether a form's campaign-wide response_limit is reached/closed."""
    form_settings = getattr(schema, "settings", None) if schema else None
    raw_limit = getattr(form_settings, "response_limit", None) if form_settings else None
    if raw_limit is None:
        return False, 0, None
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return False, 0, None
    if limit == 0:
        return False, 0, 0
    if limit < 0:
        return True, 0, limit

    if approved_counts is not None:
        return form_response_limit_status(schema, approved_counts.get(str(form_id), 0))

    if db_pool is not None:
        db_count = await count_approved_form_responses(db_pool, form_id, strict=strict)
        return form_response_limit_status(schema, db_count)

    if strict:
        raise RuntimeError("Submission database unavailable")
    return form_response_limit_status(schema, None)
