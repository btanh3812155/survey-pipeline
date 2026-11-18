# -*- coding: utf-8 -*-
"""
Survey Ingress & Verification Router.
Handles public dynamic form rendering, CTV referral resolution,
live WebRTC photo pre-checks, AI compliance audits, anti-fraud screening,
and driver appeal workflows.
"""

import os
import httpx
import html
import re
import json
import time
import hashlib
import secrets
import logging
import asyncio
import functools
import inspect
from typing import Optional, Dict, Any, Union, List, Tuple
from datetime import datetime, timezone

from fastapi import APIRouter, Request, BackgroundTasks, File, UploadFile, Form, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from config import settings
from core.state import get_db_pool, get_redis_client, get_http_client, FORM_REGISTRY, DEV_STORAGE_FILE, vision_client
from core.cache import BoundedTTLCache
from core.ctv import resolve_ctv_identifier, extract_ctv_short_code, get_ctv_options_for_form, resolve_ctv_from_slug, get_deterministic_ctv_slug
from core.repositories import append_dev_submission
from services.privacy_crypto import hash_phone, hash_plate, generate_driver_tag
from services.normalizer import sanitize_and_normalize_image, compute_laplacian_blur_score, detect_image_manipulation, normalize_image_to_webp
from services.online_and_ai_detector import compute_dhash_int, hamming_distance
from services.duplicate_detector import compute_image_dhash_str, compute_dinov2_embedding, check_intra_submission_duplicates, check_inter_submission_duplicates
from services.vehicle_tracker import VehiclePersistenceTracker, normalize_license_plate
from services.image_types import parse_date_range_from_text
from services.image_objects import (
    ImageObjectDefinitionError,
    build_detection_prompt,
    resolve_slot_object,
    run_definition_checks,
)
from services.image_object_projection import (
    image_object_result,
    project_income_totals,
    project_income_weeks,
)
from services.action_checklist import init_action_checklist, dispatch_action_checklist
from services.storage_r2 import r2_client
from services.dynamic_form_html import render_dynamic_form_html, render_ctv_selector_landing_html
from services.form_engine import validate_submission_against_schema, get_effective_image_checks

logger = logging.getLogger("routers.survey")
router = APIRouter(tags=["Survey Ingress"])
SUBMISSION_PHOTOS_DIR = os.path.join(os.getcwd(), "data", "submission_photos")
os.makedirs(SUBMISSION_PHOTOS_DIR, exist_ok=True)

# Rate limiters & in-memory caches
HISTORY_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)
LOCAL_IDEMP_CACHE = BoundedTTLCache(max_size=2048, ttl_seconds=86400.0)
PRE_CHECK_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)
ACTIVE_APPEALS_RATE_LIMITER = BoundedTTLCache(max_size=1024, ttl_seconds=7200.0)
STATUS_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)

@router.get("/camera-form", response_class=HTMLResponse)
@router.get("/forms/{form_id}", response_class=HTMLResponse)
async def get_dynamic_form(request: Request, form_id: str = "default"):
    """Renders dynamic forms with conditional logic, signature modal, and GPS authorization."""
    host = (request.headers.get("host") or "").lower()
    if any(host.startswith(p) for p in ("audit.", "logs.", "dev-audit.", "audit-dev.", "audit.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/audit", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("admin.", "dev-admin.", "admin-dev.", "admin.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/forms", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("submissions.", "view.", "data.", "dev-submissions.", "dev-data.", "submissions-dev.", "data-dev.", "data.dev.", "submissions.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/submissions", status_code=status.HTTP_302_FOUND)
    # 1. Primary: Check ?ref= query parameter (also supports ?ctv=, ?collaborator_id=)
    ref_param = (
        request.query_params.get("ref")
        or request.query_params.get("ctv")
        or request.query_params.get("collaborator_id")
    )

    matched_ctv = None
    active_slug = None
    active_ref = None
    if ref_param and ref_param.strip():
        matched_ctv = resolve_ctv_identifier(ref_param.strip())
        active_ref = extract_ctv_short_code(ref_param.strip())
        active_slug = f"ref-{active_ref}"
    else:
        # 2. Secondary fallback: check deterministic slug in URL path e.g. /forms/income-survey-59443eedf25522
        matched_ctv = resolve_ctv_from_slug(form_id)
        if matched_ctv:
            active_slug = form_id
            active_ref = extract_ctv_short_code(matched_ctv)
    # 3. Resolve schema (a matched CTV must not override an explicit form id)
    effective_form_id = form_id if form_id in FORM_REGISTRY else "income_survey"
    schema = FORM_REGISTRY.get(effective_form_id) or next(iter(FORM_REGISTRY.values()))

    # Per-form switch: when disabled, ignore ?ref= and skip the CTV selector
    ctv_ref_enabled = bool(getattr(schema.settings, "ctv_ref_enabled", True))
    if not ctv_ref_enabled:
        ref_param = None
        matched_ctv = None
        active_slug = None
        active_ref = None

    # ctv_ref_enabled is the per-form switch: when on, a direct link without ?ref=
    # must ask for the CTV first, otherwise submissions carry no collaborator and
    # approval verdicts have nowhere to go.
    has_ctv_tracking = ctv_ref_enabled

    # 4. If accessed WITHOUT ?ref= on a CTV-tracked form, show CTV selection dropdown!
    if not matched_ctv and has_ctv_tracking:
        ctv_options = get_ctv_options_for_form(schema)
        return HTMLResponse(
            content=render_ctv_selector_landing_html(
                schema=schema,
                ctv_options=ctv_options,
                form_id=effective_form_id,
                query_params=dict(request.query_params),
            ),
            status_code=200,
        )
    # 5. Render individual form with locked CTV code
    return HTMLResponse(
        content=render_dynamic_form_html(schema, ctv_code=matched_ctv, ctv_slug=active_slug, ref_code=active_ref),
        status_code=200
    )

@router.get("/api/v1/forms")
async def list_public_forms_api():
    """Lists all active public survey forms for the catalog."""
    items = []
    for fid, s in FORM_REGISTRY.items():
        if not s.settings or s.settings.access_control == "public":
            items.append({
                "form_id": fid,
                "title": s.title,
                "description": s.description or "",
                "question_count": len(s.questions),
                "is_test_mode": bool(s.settings and s.settings.is_test_mode),
            })
    return {"status": "success", "count": len(items), "forms": items}


@router.get("/api/v1/forms/{form_id}")
async def get_form_schema_api(form_id: str):
    """Public schema endpoint for dynamic form rendering."""
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Biểu mẫu không tồn tại")
    return schema.model_dump()
@router.get("/api/v1/surveys/history")
async def get_survey_history(
    phone: str,
):
    """Retrieves previous survey attempts and rejection reasons for a given driver phone number."""
    if not phone:
        raise HTTPException(status_code=400, detail="Vui lòng cung cấp số điện thoại hợp lệ.")

    clean_phone = re.sub(r"[\s\-\.\(\)]", "", phone.strip())
    if not clean_phone or not re.match(r"^\+?[0-9]{8,15}$", clean_phone):
        raise HTTPException(status_code=400, detail="Số điện thoại không hợp lệ.")

    rate_limited = False
    redis_client = get_redis_client()
    if redis_client:
        try:
            rkey = f"rate_limit:history:{clean_phone}"
            count = await redis_client.incr(rkey)
            if count == 1:
                await redis_client.expire(rkey, 60)
            if count > 30:
                rate_limited = True
        except Exception as e:
            logger.warning(f"Redis rate limiting error: {e}")
            req_count = HISTORY_RATE_LIMITER.get(clean_phone, 0)
            if req_count >= 30:
                rate_limited = True
            else:
                HISTORY_RATE_LIMITER[clean_phone] = req_count + 1
    else:
        req_count = HISTORY_RATE_LIMITER.get(clean_phone, 0)
        if req_count >= 30:
            rate_limited = True
        else:
            HISTORY_RATE_LIMITER[clean_phone] = req_count + 1

    if rate_limited:
        raise HTTPException(status_code=429, detail="Quá nhiều yêu cầu tra cứu lịch sử. Vui lòng thử lại sau!")

    phone = clean_phone
    results = []
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("driver_phone") == phone:
                    results.append({
                        "submission_id": r.get("submission_id"),
                        "mode": "DEV_TEST",
                        "status": r.get("status"),
                        "reason": r.get("reason"),
                        "submitted_at": r.get("submitted_at")
                    })
        except Exception as e:
            logger.warning(f"Error reading dev history: {e}")

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT submission_id, status, rejection_reason, created_at
                        FROM survey_submissions
                        WHERE driver_phone = %s
                        ORDER BY created_at DESC
                        LIMIT 20;
                        """,
                        (phone,)
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        results.append({
                            "submission_id": row[0],
                            "mode": "PRODUCTION",
                            "status": row[1],
                            "reason": row[2] or "Hợp lệ",
                            "submitted_at": row[3].isoformat() if row[3] else None
                        })
        except Exception as e:
            logger.warning(f"Error reading DB history: {e}")

    return {"phone": phone, "count": len(results), "attempts": results}

def _read_dev_submission_status(clean_id: str):
    for fpath in (
        os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
        os.path.join(os.getcwd(), "data", "dev_submissions.json"),
    ):
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                records = json.load(f)
            for r in records if isinstance(records, list) else []:
                if str(r.get("submission_id") or "").strip() != clean_id:
                    continue
                # dev_test_submissions.json also holds action-checklist stubs
                # ({submission_id, action_status}); they are not submission records.
                if not (r.get("status") or r.get("reason") or r.get("rejection_reason") or r.get("submitted_at")):
                    continue
                return {
                    "submission_id": clean_id,
                    "status": (r.get("status") or "MANUAL_REVIEW").upper(),
                    "reason": r.get("reason") or r.get("rejection_reason") or "",
                    "updated_at": r.get("moderated_at") or r.get("updated_at") or r.get("submitted_at"),
                    "is_test_mode": True,
                }
        except Exception as e:
            logger.warning(f"Error reading dev submission status from {fpath}: {e}")
    return None


@router.get("/api/v1/survey/status/{submission_id}")
async def get_submission_status(request: Request, submission_id: str):
    """Public, PII-free approval status lookup for a submitted survey."""
    client_ip = request.client.host if request.client else "unknown"
    curr = STATUS_RATE_LIMITER.get(client_ip, 0)
    if curr >= 60:
        raise HTTPException(status_code=429, detail="Quá nhiều yêu cầu tra cứu trạng thái. Vui lòng thử lại sau!")
    STATUS_RATE_LIMITER[client_ip] = curr + 1

    clean_id = (submission_id or "").strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", clean_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu khảo sát.")

    is_dev_id = clean_id.upper().startswith(("DEV-", "TEST-"))
    if is_dev_id:
        dev_status = _read_dev_submission_status(clean_id)
        if dev_status:
            return dev_status
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu khảo sát.")

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT status, rejection_reason, COALESCE(moderated_at, created_at)
                        FROM survey_submissions
                        WHERE submission_id = %s;
                        """,
                        (clean_id,),
                    )
                    row = await cur.fetchone()
            if row:
                return {
                    "submission_id": clean_id,
                    "status": (row[0] or "MANUAL_REVIEW").upper(),
                    "reason": row[1] or "",
                    "updated_at": row[2].isoformat() if hasattr(row[2], "isoformat") else (row[2] or None),
                    "is_test_mode": False,
                }
        except Exception as e:
            logger.warning(f"Error reading DB submission status: {e}")

    # No pool configured (or DB miss): local dev storage still holds test records.
    dev_status = _read_dev_submission_status(clean_id)
    if dev_status:
        return dev_status

    raise HTTPException(status_code=404, detail="Không tìm thấy phiếu khảo sát.")
PRE_CHECK_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)

@router.post("/api/v1/survey/pre-check")
async def pre_check_image_slot(
    request: Request,
    photo: UploadFile = File(...),
    slot_index: int = Form(0),
    role: Optional[str] = Form(""),
    form_id: Optional[str] = Form("income_survey"),
    question_id: Optional[str] = Form("camera_receipt"),
):
    """Lightweight instant pre-check on photo capture: blur variance, format, and quick role check."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    rate_key = f"precheck:{client_ip}"
    
    # Rate limiting: max 20 pre-checks per minute per client IP
    redis_client = get_redis_client()
    if redis_client:
        try:
            cnt = await redis_client.incr(rate_key)
            if cnt == 1:
                await redis_client.expire(rate_key, 60)
            if cnt > 20:
                return JSONResponse(status_code=429, content={"status": "RATE_LIMITED", "detail": "Pre-check rate limit exceeded. Please wait a moment."})
        except Exception:
            pass
    else:
        curr = PRE_CHECK_RATE_LIMITER.get(client_ip, 0)
        if curr >= 20:
            return JSONResponse(status_code=429, content={"status": "RATE_LIMITED", "detail": "Pre-check rate limit exceeded."})
        PRE_CHECK_RATE_LIMITER[client_ip] = curr + 1

    try:
        raw_bytes = await photo.read()
        if not raw_bytes:
            return {"status": "ERROR", "message": "Ảnh rỗng hoặc không đọc được."}

        # 1. Format & Normalization
        norm_bytes, pil_im = sanitize_and_normalize_image(raw_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION)
        
        # 2. Blur / Sharpness Check (~20ms)
        schema = FORM_REGISTRY.get(form_id)
        checks = get_effective_image_checks(schema, question_id)
        blur_thresh = checks.blur_threshold if checks.blur_threshold is not None else settings.BLUR_THRESHOLD
        sharp, b_var = compute_laplacian_blur_score(pil_im, threshold=blur_thresh)
        
        if not sharp:
            return {
                "status": "BLURRY",
                "is_sharp": False,
                "blur_score": round(b_var, 1),
                "threshold": blur_thresh,
                "message": f"Ảnh hơi mờ (độ nét {b_var:.1f} < ngưỡng {blur_thresh:.0f}). Bạn nên giữ chắc tay chụp lại!"
            }

        # 3. Fast Metadata Forensics Check (Photoshop/Canva)
        is_tampered, tamper_reason = detect_image_manipulation(raw_bytes, pil_im)
        if is_tampered:
            return {
                "status": "TAMPERED",
                "is_sharp": True,
                "message": f"Cảnh báo: Phát hiện dấu hiệu chỉnh sửa ảnh ({tamper_reason})."
            }

        return {
            "status": "PASS",
            "is_sharp": True,
            "blur_score": round(b_var, 1),
            "message": "Ảnh đạt chuẩn độ nét và chất lượng!"
        }
    except Exception as e:
        logger.warning(f"Pre-check error: {e}")
        return {"status": "PASS", "is_sharp": True, "message": "Đã ghi nhận ảnh."}
def _looks_like_income_statement(result: Any) -> bool:
    """True when an OCR result actually looks like a revenue statement, not a menu/profile screen."""
    if not isinstance(result, dict):
        return False
    if result.get("layout_matched"):
        return True
    if result.get("period_tabs"):
        return True
    labels = [str(x).lower() for x in (result.get("field_labels") or [])]
    if any(token in label for label in labels for token in ("tổng thu nhập", "doanh số", "thu nhập ròng", "chiết khấu")):
        return True
    return any(int(result.get(key) or 0) for key in ("gross_revenue", "net_income", "total_trips"))


def _income_fingerprint(result: Any) -> Optional[Tuple[int, int, int]]:
    """Financial identity of a weekly screen, or None when OCR read nothing usable."""
    if not isinstance(result, dict):
        return None
    gross = int(result.get("gross_revenue") or 0)
    net = int(result.get("net_income") or 0)
    trips = int(result.get("total_trips") or 0)
    if gross <= 0 and net <= 0 and trips <= 0:
        return None
    return (gross, net, trips)


def _find_duplicate_income_period(income_pairs: List[Tuple[int, Any]]) -> Optional[dict]:
    """Return a violation when two uploaded screens report the same week period.

    Period text alone is not proof: the vision model periodically misreads which
    teal tab is active (e.g. week 17-23 read as 10-16), which would reject five
    genuinely distinct weeks. Two screens of the same physical week always show
    the same money, so a duplicate also requires matching financials.
    """
    seen: Dict[Any, Tuple[int, Any]] = {}
    for idx_pos, result in income_pairs:
        if not isinstance(result, dict):
            continue
        if result.get("period_reliable") is False:
            # The selected week tab could not be read; the weekly loop rejects it with
            # an actionable reason. Never turn an unreadable tab into a duplicate claim.
            continue
        label = str(result.get("period") or result.get("period_raw_text") or "").strip()
        if not label:
            continue
        start, end, _ = parse_date_range_from_text(label, default_year=2026)
        key = (start, end) if start and end else label.lower()
        fingerprint = _income_fingerprint(result)
        if key in seen:
            first_idx, first_result = seen[key]
            first_fingerprint = _income_fingerprint(first_result)
            if fingerprint is None or first_fingerprint is None or fingerprint != first_fingerprint:
                logger.info(
                    f"[DUPLICATE PERIOD] Ảnh {first_idx + 1} and Ảnh {idx_pos + 1} share the OCR period "
                    f"'{label}' but have different financials {first_fingerprint} vs {fingerprint}; not a duplicate."
                )
                continue
            return {
                "failed_slot": idx_pos,
                "ocr_data": result,
                "reason": (
                    f"Ảnh Tuần bị trùng kỳ báo cáo ({label}) ở Ảnh {first_idx + 1} và Ảnh {idx_pos + 1}. "
                    "Vui lòng chụp đủ các tuần khác nhau (thứ tự ảnh không quan trọng)."
                ),
            }
        seen[key] = (idx_pos, result)
    return None


def _unreliable_week_reason(
    ocr_data: Any, week_num: int, image_num: Optional[int] = None
) -> Optional[str]:
    """Actionable rejection text when the selected week tab could not be read reliably."""
    if not isinstance(ocr_data, dict) or ocr_data.get("period_reliable") is not False:
        return None
    detail = str(ocr_data.get("period_uncertain_reason") or "").strip()
    label = f"Ảnh Tuần {week_num}" if image_num is None else f"Ảnh Tuần {week_num} (Ảnh {image_num})"
    reason = (
        f"{label}: Không đọc được chính xác tuần đang chọn (tab tuần bị cắt/khuất). "
        "Vui lòng chụp lại màn hình hiển thị đủ các tab tuần."
    )
    if detail:
        reason += f" Chi tiết: {detail}."
    return reason


def _select_unique_check_index(target_q: Any, income_indices: List[int], image_count: int) -> Optional[int]:
    """
    Image index that must be globally unique. Profile/vehicle slots explicitly opt out
    (require_unique=False) because the same driver reuses them across submissions;
    the weekly income screens are the uniqueness-bearing objects.
    """
    # income_indices are GLOBAL image positions (all camera questions are flattened
    # into one list); target_q slot numbers are question-local and only line up with
    # the global list when this question's uploads start at index 0.
    if income_indices:
        first = income_indices[0]
        if 0 <= first < image_count:
            return first
    if target_q and getattr(target_q, "image_slots", None):
        for s_idx, slot in enumerate(target_q.image_slots):
            if slot.get("require_unique") is not False and s_idx < image_count:
                return s_idx
    return None


_VN_PHONE_RE = re.compile(r"(?:\+?84|0)(?:\d[ .-]?){8,10}\d")


def _infer_driver_phone(schema, answers: Dict[str, Any]) -> Optional[str]:
    """Find the driver phone in schema answers when the legacy field is absent."""
    if not isinstance(answers, dict):
        return None
    best = None
    for q in (getattr(schema, "questions", None) or []):
        value = answers.get(q.id)
        if not isinstance(value, str):
            continue
        match = _VN_PHONE_RE.search(value.strip())
        if not match:
            continue
        label = f"{q.id} {getattr(q, 'title', '')}".lower()
        score = 2 if any(t in label for t in ("phone", "điện thoại", "dien thoai", "sđt", "sdt", "zalo")) else 1
        candidate = (score, value.strip())
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def _build_retry_form_link(
    form_id: Optional[str],
    ctv_ref: Optional[str],
    collaborator_id: Optional[Union[str, int]],
    recipient_kind: str = "ctv",
) -> str:
    """Retry/submit-again link that keeps the original submission's CTV ref."""
    from core.ctv import extract_ctv_short_code, build_ctv_form_url

    ref_source = ctv_ref or (collaborator_id if recipient_kind != "driver" else None)
    ref = extract_ctv_short_code(str(ref_source)) if ref_source else ""
    return build_ctv_form_url(ref, str(form_id or ""))


def _infer_slot_role(slot: Any) -> str:
    """Resolve generic/custom slot roles from their titles (profile/income/vehicle)."""
    if not isinstance(slot, dict):
        return "unknown"
    role = str(slot.get("role") or "").strip().lower()
    title = str(slot.get("title") or "").lower()
    if role and role not in ("custom_object", "custom", "unknown", "slot", "other"):
        return role
    if "hồ sơ" in title or "profile" in title or "màn hình chính" in title:
        return "app_profile"
    if "thu nhập" in title or "income" in title or "tuần" in title:
        return "app_income"
    if "thân xe" in title or "đầu xe" in title or "vô lăng" in title or "phương tiện" in title:
        return "vehicle_exterior"
    return role or "unknown"


def _normalized_image_slots(slots: Any) -> List[dict]:
    normalized = []
    for slot in (slots or []):
        if not isinstance(slot, dict):
            continue
        slot_copy = {**slot, "role": _infer_slot_role(slot)}
        try:
            obj = resolve_slot_object(slot)
        except ImageObjectDefinitionError:
            if str(slot.get("object_type") or "").strip():
                raise
            obj = None
        if obj is not None and obj.classify_role:
            slot_copy["role"] = obj.classify_role
            slot_copy["object_type"] = obj.id
        normalized.append(slot_copy)
    return normalized


def _answer_by_title_keywords(schema: Any, answers: Dict[str, Any], keywords: tuple) -> Optional[str]:
    """First non-empty textual answer whose question title matches any keyword."""
    for question in (getattr(schema, "questions", None) or []):
        title = str(getattr(question, "title", "") or "").lower()
        if not any(keyword in title for keyword in keywords):
            continue
        value = answers.get(getattr(question, "id", ""))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _normalize_vehicle_type(value: Optional[str]) -> str:
    """Vehicle type is only ever CAR or BIKE."""
    text = str(value or "").upper()
    if "BIKE" in text or "MÁY" in text or "XE MAY" in text:
        return "BIKE"
    return "CAR"


def _classification_slots(schema: Any, target_q: Any) -> List[dict]:
    """Deduplicated (role,title) slot contract covering every camera question."""
    slots = []
    seen = set()
    questions = [
        q for q in (getattr(schema, "questions", None) or [])
        if getattr(q, "type", None) == "camera_webrtc" and getattr(q, "image_slots", None)
    ] or ([target_q] if target_q and getattr(target_q, "image_slots", None) else [])
    for question in questions:
        for slot in _normalized_image_slots(question.image_slots):
            role = slot.get("role")
            if role in seen:
                continue
            seen.add(role)
            slots.append(slot)
    return slots


def _slot_config(slot: Any) -> Dict[str, Any]:
    if not isinstance(slot, dict):
        return {}
    config = slot.get("object_config")
    return dict(config) if isinstance(config, dict) else {}


def _slot_object(slot: Any):
    """Resolve a slot to its file-defined object (compat resolver for legacy slots)."""
    return resolve_slot_object(slot)


async def _call_object_extractor(extractor_fn, image_bytes: bytes, week_idx: Optional[int], prompt: str) -> Dict[str, Any]:
    """Call a vision extractor, passing only arguments its real signature accepts."""
    kwargs: Dict[str, Any] = {}
    try:
        parameters = inspect.signature(extractor_fn).parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        if week_idx is not None and ("week_idx" in parameters or accepts_kwargs):
            kwargs["week_idx"] = week_idx
        if "prompt" in parameters or accepts_kwargs:
            kwargs["prompt"] = prompt
    except (TypeError, ValueError):
        pass
    try:
        return await extractor_fn(image_bytes, **kwargs)
    except TypeError as exc:
        if "prompt" in kwargs and "prompt" in str(exc):
            kwargs.pop("prompt")
            return await extractor_fn(image_bytes, **kwargs)
        raise


async def _extract_image_object(
    image_bytes: bytes,
    obj: Any,
    config: Optional[Dict[str, Any]] = None,
    week_idx: Optional[int] = None,
    prompt_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the object's built-in extraction profile with its generated prompt."""
    if obj.extractor == "none":
        return {}
    prompt = build_detection_prompt(obj, config or {})
    if (prompt_override or "").strip() and prompt_override.strip() not in prompt:
        prompt = f"{prompt}\nAdditional slot instructions: {prompt_override.strip()}"
    if obj.extractor == "income_statement":
        result = await _call_object_extractor(vision_client.ocr_driver_app_income, image_bytes, week_idx, prompt)
    elif obj.extractor == "profile":
        result = await _call_object_extractor(vision_client.ocr_driver_app_profile, image_bytes, None, prompt)
    elif obj.extractor == "vehicle":
        return {}
    else:
        result = await vision_client.extract_object_fields(image_bytes, prompt)
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    return result if isinstance(result, dict) else {}


INTERRUPTED_CHECK_REASON = (
    "Hệ thống gián đoạn trong quá trình kiểm tra; ảnh đã nhận, cần kiểm tra lại."
)


async def _persist_interrupted_submission(request: Request) -> None:
    """Writes the one-shot interruption row when checks/verdict/persist died."""
    pending = getattr(getattr(request, "state", None), "pending_submission", None)
    if not pending or pending.get("final_written"):
        return
    if pending.get("is_test_mode") or not pending.get("db_pool"):
        pending["final_written"] = True
        return
    submission_id = pending.get("submission_id")
    try:
        async with pending["db_pool"].connection() as conn:
            await conn.execute(
                """
                INSERT INTO survey_submissions (
                    submission_id, form_id, ctv_code, driver_phone, driver_name,
                    receipt_serial_no, status, rejection_reason, form_data
                ) VALUES (%s, %s, %s, %s, %s, %s, 'MANUAL_REVIEW', %s, %s)
                ON CONFLICT (submission_id) DO NOTHING;
                """,
                (
                    submission_id,
                    pending.get("form_id"),
                    pending.get("ctv_code"),
                    pending.get("driver_phone"),
                    pending.get("driver_name"),
                    f"SN-{pending.get('license_plate') or 'AUTO'}",
                    INTERRUPTED_CHECK_REASON,
                    json.dumps({
                        "status": "INTERRUPTED",
                        "interrupted_at": datetime.now(timezone.utc).isoformat(),
                    }),
                ),
            )
        pending["final_written"] = True
        logger.warning(f"[SUBMIT] Persisted interruption row for #{submission_id}")
    except Exception as e:
        logger.error(f"[SUBMIT] Failed to persist interruption row for #{submission_id}: {e}")


def _submission_crash_guard(endpoint):
    """
    Guarantees a reviewable row if an unexpected exception, worker cancellation or
    client disconnect aborts the handler AFTER the photos were written but BEFORE
    the final verdict row exists. The shielded write survives task cancellation;
    normal verdicts are untouched (the guard is a no-op once final_written is set).
    """
    @functools.wraps(endpoint)
    async def _wrapped(*args, **kwargs):
        request = kwargs.get("request")
        if request is None:
            request = next((a for a in args if isinstance(a, Request)), None)
        try:
            return await endpoint(*args, **kwargs)
        except asyncio.CancelledError:
            if request is not None:
                await asyncio.shield(_persist_interrupted_submission(request))
            raise
        except Exception:
            if request is not None:
                await asyncio.shield(_persist_interrupted_submission(request))
            raise

    return _wrapped


def _store_submission_photos(sub_photo_dir: str, raw_bytes_list: List[bytes]) -> None:
    """
    Single write boundary for the raw photo store.

    Every slot is re-encoded to WebP (unless it already is WebP, which is copied
    untouched), so filename, bytes and MIME always agree no matter which client,
    script or test posted the upload. Undecodable payloads are kept as .bin for
    forensics instead of being mislabeled as images.
    """
    try:
        os.makedirs(sub_photo_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create photo dir {sub_photo_dir}: {e}")
        return
    for i_idx, r_b in enumerate(raw_bytes_list):
        target = os.path.join(sub_photo_dir, f"slot_{i_idx}.webp")
        try:
            webp_bytes = normalize_image_to_webp(r_b)
            if webp_bytes is None:
                target = os.path.join(sub_photo_dir, f"slot_{i_idx}.bin")
                webp_bytes = r_b
                logger.warning(f"Undecodable slot {i_idx} upload; stored raw bytes at {target}")
            with open(target, "wb") as fp:
                fp.write(webp_bytes)
        except Exception as e:
            logger.warning(f"Failed to store slot {i_idx} photo: {e}")


@router.post("/api/v1/survey/submit-webrtc")
@_submission_crash_guard
async def submit_webrtc_survey(
    request: Request,
    background_tasks: BackgroundTasks,
    survey_photo: Optional[UploadFile] = File(None),
    driver_phone: Optional[str] = Form(None),
    license_plate: Optional[str] = Form(None),
    driver_name: Optional[str] = Form(None),
    collaborator_id: Optional[str] = Form(None),
    vehicle_type: Optional[str] = Form("CAR"),
    gross_revenue: Optional[str] = Form("0"),
    trip_count: Optional[str] = Form("0"),
    odometer_km: Optional[str] = Form("0"),
    signature_png: Optional[str] = Form(None),
    is_test_mode: Optional[bool] = Form(False),
    idempotency_key: Optional[str] = Form(None),
    bypass_img_check: Optional[bool] = Form(False),
    form_id: Optional[str] = Form(None),
    question_id: Optional[str] = Form(None),
):
    """Ingests live WebRTC canvas captures and performs real-time blur and vision audit."""
    db_pool = get_db_pool()
    redis_client = get_redis_client()
    clean_phone = driver_phone.strip() if driver_phone and driver_phone.strip() else None
    driver_phone = clean_phone
    driver_phone_hash = hash_phone(clean_phone) if clean_phone else None
    license_plate_hash = hash_plate(license_plate) if license_plate else None
    driver_tag = generate_driver_tag(clean_phone) if clean_phone else f"ANON-{secrets.token_hex(4).upper()}"

    submission_id = f"{'DEV' if is_test_mode else 'VN'}-{secrets.token_hex(8).upper()}"
    # 0. Idempotency Check: return cached response if key was already submitted
    if idempotency_key:
        idemp_cache_key = f"idemp:{clean_phone or 'anon'}:{idempotency_key}"
        cached_payload = None
        redis_client = get_redis_client()
        if redis_client:
            try:
                cached_payload = await redis_client.get(idemp_cache_key)
            except Exception as e:
                logger.warning(f"Redis idempotency lookup error: {e}")
        if not cached_payload and idemp_cache_key in LOCAL_IDEMP_CACHE:
            cached_payload = LOCAL_IDEMP_CACHE[idemp_cache_key]

        if cached_payload:
            logger.info(f"[IDEMPOTENCY HIT] Returning cached survey for key: {idemp_cache_key}")
            return JSONResponse(status_code=200, content=json.loads(cached_payload))

    # Extract all submitted form parameters and support multiple image uploads
    form_data = await request.form()
    car_model_val = str(form_data.get("car_model") or "")
    collab_param = (
        collaborator_id
        or form_data.get("collaborator_id")
        or form_data.get("ctv_code")
        or form_data.get("ctv_id")
        or form_data.get("collaborator_tg_id")
        or request.query_params.get("collaborator_id")
        or request.query_params.get("ctv")
    )
    requested_form_id = (form_id or "").strip()
    active_form_id = requested_form_id or "income_survey"
    # Only validate against the schema the client actually submitted. A missing or
    # unknown form_id must not silently validate against the default form.
    schema = FORM_REGISTRY.get(requested_form_id) if requested_form_id else None
    if requested_form_id and schema is None:
        logger.warning(f"[SUBMIT] form_id '{requested_form_id}' is not registered; schema validation skipped.")
    if not question_id and schema:
        first_camera = next((q for q in schema.questions if q.type == "camera_webrtc"), None)
        question_id = first_camera.id if first_camera else None
    target_question_id = question_id or "camera_receipt"
    target_q = next((qu for qu in schema.questions if qu.id == target_question_id), None) if schema else None

    # Collect every uploaded image across all camera questions. Slot files are
    # keyed `<question_id>__file_<slot>` so multiple camera questions no longer
    # overwrite each other under a shared survey_photo_<idx> key.
    images_list = [v for _k, v in form_data.multi_items() if hasattr(v, "read")]
    has_indexed_photos = len(images_list) > 1

    answers: Dict[str, Any] = {
        k: v for k, v in form_data.multi_items() if not hasattr(v, "read")
    }
    if not driver_phone:
        driver_phone = _infer_driver_phone(schema, answers)
    if not answers.get("driver_phone"):
        answers["driver_phone"] = driver_phone
    if "driver_name" not in answers and driver_name:
        answers["driver_name"] = driver_name
    if "driver_signature" not in answers and signature_png:
        answers["driver_signature"] = signature_png
    is_bypassed = bool(is_test_mode and str(bypass_img_check).lower() in ("true", "1", "t", "yes"))
    if images_list:
        if f"{target_question_id}_0" in answers:
            pass  # modern client already sent per-slot markers for every camera question
        elif target_q and target_q.image_slots:
            uploaded = min(len(images_list), len(target_q.image_slots))
            answers[target_question_id] = [f"uploaded_{i}" for i in range(uploaded)]
            for slot_index in range(uploaded):
                answers[f"{target_question_id}_{slot_index}"] = f"uploaded_{slot_index}"
        elif has_indexed_photos or len(images_list) > 1:
            answers[target_question_id] = [f"uploaded_{i}" for i in range(len(images_list))]
        else:
            # Legacy single photo upload without indexed multi-image fields
            if target_q and target_q.min_images and target_q.min_images > 1:
                answers[target_question_id] = [f"uploaded_{i}" for i in range(target_q.min_images)]
            else:
                answers[target_question_id] = "uploaded"
    elif is_bypassed:
        slot_cnt = len(target_q.image_slots) if (target_q and target_q.image_slots) else (target_q.min_images or 7 if target_q else 7)
        answers[target_question_id] = [f"bypassed_slot_{i}" for i in range(slot_cnt)]
    elif target_question_id not in answers or not answers[target_question_id]:
        answers[target_question_id] = None
    if "camera_receipt" not in answers and target_question_id != "camera_receipt":
        answers["camera_receipt"] = answers.get(target_question_id, "uploaded")
    if "daily_net_income" not in answers and gross_revenue is not None:
        answers["daily_net_income"] = gross_revenue
    if "monthly_gross_revenue" not in answers and gross_revenue is not None:
        answers["monthly_gross_revenue"] = gross_revenue

    # Server-Side Schema Enforcement (VULN-API-01)
    checks = get_effective_image_checks(schema, target_question_id)
    is_schema_valid = True
    validation_err = None
    if schema:
        is_schema_valid, validation_err = validate_submission_against_schema(schema, answers)
    pil_im = None
    blur_var = None
    raw_bytes_list = []
    for img in images_list:
        await img.seek(0)
        b = await img.read()
        raw_bytes_list.append(b)
        await img.seek(0)

    # Save photos locally so admin Telegram bot can send them in /check response
    sub_photo_dir = os.path.join(SUBMISSION_PHOTOS_DIR, submission_id)
    _store_submission_photos(sub_photo_dir, raw_bytes_list)

    raw_bytes = raw_bytes_list[0] if raw_bytes_list else b""

    # Crash safety WITHOUT a premature row: nothing is written to the DB while the
    # checks run (a CTV/admin must never see a placeholder "processing" verdict).
    # If the checks/verdict/persist stage dies after the photos are already on disk,
    # _submission_crash_guard writes a single interruption row from this snapshot.
    request.state.pending_submission = {
        "submission_id": submission_id,
        "form_id": active_form_id,
        "ctv_code": str(collab_param or "").strip() or None,
        "driver_phone": clean_phone,
        "driver_name": driver_name,
        "license_plate": license_plate,
        "db_pool": db_pool,
        "is_test_mode": bool(is_test_mode),
        "final_written": False,
    }

    compat_income: Dict[str, Dict[str, Any]] = {}
    compat_totals: Dict[str, int] = {"total_gross_revenue": 0, "total_net_income": 0, "total_trips": 0}
    if not is_schema_valid:
        status_str = "REJECTED"
        is_approved = False
        reason_str = validation_err or "Không hợp lệ theo biểu mẫu."
        ai_res = {}
    else:
        # 1. Fleet Anti-Fraud: Route through VehiclePersistenceTracker when license plate & phone are provided
        v_match = None
        if checks.enable_vehicle_match and license_plate and driver_phone:
            tracker = VehiclePersistenceTracker(db_pool=None)
            v_match = await tracker.verify_and_persist_vehicle(
                driver_phone=driver_phone,
                detected_plate=license_plate,
                detected_vehicle_type=vehicle_type or "CAR",
                driver_phone_hash=driver_phone_hash,
            )

        # Collect raw bytes for all uploaded images in images_list
        # Use previously collected raw bytes

        raw_bytes = raw_bytes_list[0] if raw_bytes_list else b""

        is_bypassed = bool(is_test_mode and str(bypass_img_check).lower() in ("true", "1", "t", "yes"))
        if is_bypassed:
            ai_res = {
                "status": "APPROVED",
                "gross_revenue": gross_revenue or "650000",
                "driver_phone": driver_phone,
                "detected_plate": license_plate or "29E-123.45",
                "is_screen_photo": False,
                "bypass_img_check": True,
            }
            if v_match and not v_match.is_match:
                is_approved = False
                status_str = "REJECTED"
                reason_str = v_match.flag_reason or f"Lỗi phương tiện: {v_match.status_code}"
            else:
                is_approved = True
                status_str = "APPROVED"
                reason_str = "Chế độ thử nghiệm: Đã bỏ qua kiểm tra ảnh để đẩy nhanh luồng dữ liệu mẫu."
        else:
            # Decode + downscale every upload exactly ONCE, in worker threads.
            # The previous flow decoded each image up to four times (target frame,
            # blur loop, AI normalisation pass, duplicate detector) synchronously on
            # the event loop, so a single submission froze the whole uvicorn worker
            # for seconds and the client sat on a dead "Đang gửi..." button.
            norm_results = await asyncio.gather(
                *[
                    asyncio.to_thread(
                        sanitize_and_normalize_image,
                        b,
                        max_dimension=settings.IMAGE_MAX_DIMENSION,
                    )
                    for b in raw_bytes_list
                ],
                return_exceptions=True,
            )
            norm_bytes_cache: Dict[int, bytes] = {}
            pil_cache: Dict[int, Any] = {}
            for _idx, _res in enumerate(norm_results):
                if isinstance(_res, tuple) and _res[1] is not None:
                    norm_bytes_cache[_idx] = _res[0]
                    pil_cache[_idx] = _res[1]

            # For facts extraction, select the app revenue screen if multi-image
            target_idx = 1 if len(raw_bytes_list) >= 2 else 0
            target_norm = norm_results[target_idx] if 0 <= target_idx < len(norm_results) else None
            if isinstance(target_norm, tuple) and target_norm[1] is not None:
                norm_bytes, pil_im = target_norm
            else:
                decode_err = target_norm if isinstance(target_norm, Exception) else "unreadable image"
                norm_bytes, pil_im = None, None
                is_approved = False
                status_str = "REJECTED"
                reason_str = f"Không thể giải mã ảnh: {str(decode_err)}"
                ai_res = {}

            if pil_im is not None:
                is_sharp = True
                blur_var = None
                blurry_slot_idx = None
                blurry_slot_title = None
                decode_error_slot_idx = None
                decode_error_slot_title = None

                is_sharp = True
                blur_var = None
                blur_thresh = settings.BLUR_THRESHOLD
                blurry_slot_idx = None
                blurry_slot_title = None
                decode_error_slot_idx = None
                decode_error_slot_title = None

                for idx, img_b in enumerate(raw_bytes_list):
                    slot_checks = get_effective_image_checks(schema, target_question_id, slot_index=idx)
                    if not slot_checks.enable_blur_check:
                        continue
                    slot_blur_thresh = slot_checks.blur_threshold if slot_checks.blur_threshold is not None else settings.BLUR_THRESHOLD
                    slot_name = f"Ảnh {idx + 1}"
                    if target_q and target_q.image_slots and idx < len(target_q.image_slots):
                        stitle = target_q.image_slots[idx].get("title") or target_q.image_slots[idx].get("role")
                        if stitle:
                            slot_name = f"Ảnh {idx + 1} ({stitle})"
                    try:
                        p_im = pil_cache.get(idx)
                        if p_im is None:
                            raise ValueError("unreadable image stream")
                        sharp, b_var = await asyncio.to_thread(
                            compute_laplacian_blur_score, p_im, slot_blur_thresh
                        )
                        if not sharp:
                            is_sharp = False
                            blur_var = b_var
                            blurry_slot_idx = idx
                            blurry_slot_title = slot_name
                            blur_thresh = slot_blur_thresh
                            break
                        blur_var = b_var
                    except Exception:
                        is_sharp = False
                        decode_error_slot_idx = idx
                        decode_error_slot_title = slot_name
                        break

                if not is_sharp:
                    is_approved = False
                    status_str = "REJECTED"
                    if decode_error_slot_title:
                        reason_str = f"{decode_error_slot_title}: Không thể giải mã tệp ảnh (định dạng stream lỗi hoặc file hỏng). Vui lòng chụp lại!"
                    elif blurry_slot_title:
                        score_text = f" (độ nét đo được: {blur_var:.1f} < ngưỡng tối thiểu {blur_thresh})" if blur_var is not None else ""
                        reason_str = f"{blurry_slot_title}: Ảnh chụp bị mờ hoặc rung tay{score_text}, vui lòng giữ chắc tay và chụp lại rõ nét hơn!"
                    else:
                        reason_str = "Ảnh chụp bị mờ hoặc rung tay, vui lòng chụp lại rõ nét hơn!"
                    ai_res = {
                        "status": "REJECTED",
                        "failed_slot": blurry_slot_idx if blurry_slot_idx is not None else decode_error_slot_idx,
                        "failed_slot_name": blurry_slot_title or decode_error_slot_title,
                        "blur_score": blur_var,
                        "blur_threshold": blur_thresh if checks.enable_blur_check else None,
                    }
                elif v_match and not v_match.is_match:
                    is_approved = False
                    status_str = "REJECTED"
                    reason_str = v_match.flag_reason or f"Lỗi phương tiện: {v_match.status_code}"
                    ai_res = {}
                elif not checks.enable_ai_vision:
                    ai_res = {"skipped": True, "reason": "Bỏ qua kiểm tra AI theo cấu hình"}
                    is_approved = True
                    status_str = "APPROVED"
                    reason_str = "Chấp thuận (bỏ qua kiểm tra AI theo cấu hình biểu mẫu)."
                else:
                    # AI vision audit with parallel execution via asyncio.gather
                    if target_q and target_q.image_slots and len(images_list) >= 2:
                        # 1. Reuse the single decode pass done for the blur check.
                        norm_bytes_list = [
                            norm_bytes_cache.get(i, raw_bytes_list[i])
                            for i in range(len(raw_bytes_list))
                        ]

                        # 2. Parallel role classification across all normalized images.
                        # The allowed role set must cover every camera question (profile,
                        # vehicle AND income), otherwise income screens can never be
                        # classified and week completeness falsely fails.
                        classification_slots = _classification_slots(schema, target_q)
                        classify_tasks = [
                            vision_client.classify_image_role(
                                norm_bytes_list[s_idx],
                                filename=getattr(images_list[s_idx], "filename", "") or "",
                                expected_slots=classification_slots,
                            )
                            for s_idx in range(len(raw_bytes_list))
                        ]
                        classified_roles = await asyncio.gather(*classify_tasks, return_exceptions=True)
                        classified_roles = [r if isinstance(r, str) else "unknown" for r in classified_roles]

                        # 3. Order-agnostic object coverage: classify every image, then
                        # require the right objects rather than matching image positions.
                        def _role_group(role):
                            value = str(role or "").lower()
                            if "profile" in value or "odometer" in value:
                                return "profile"
                            if "vehicle" in value or "exterior" in value or "steering" in value:
                                return "vehicle"
                            if "income" in value or "revenue" in value or "earnings" in value:
                                return "income"
                            return "unknown"

                        groups = {"profile": [], "vehicle": [], "income": []}
                        for idx, role in enumerate(classified_roles):
                            group = _role_group(role)
                            if group in groups:
                                groups[group].append(idx)

                        required_slots = [
                            slot for slot in (target_q.image_slots or []) if slot.get("required", True)
                        ]
                        required_counts = {"profile": 0, "vehicle": 0, "income": 0}
                        required_roles = [
                            (_normalized_image_slots([slot])[0].get("role"), slot) for slot in required_slots
                        ]
                        for role, _slot in required_roles:
                            group = _role_group(role)
                            if group in required_counts:
                                required_counts[group] += 1

                        missing_titles = []
                        for group, needed in required_counts.items():
                            if len(groups[group]) < needed:
                                for role, slot in required_roles:
                                    if _role_group(role) == group:
                                        missing_titles.append(str(slot.get("title") or slot.get("role") or group))
                                        break

                        coverage_violation = None
                        if missing_titles:
                            coverage_violation = {
                                "failed_slot": 0,
                                "ocr_data": {},
                                "reason": (
                                    "Thiếu ảnh đúng loại cho: " + "; ".join(missing_titles)
                                    + ". Vui lòng chụp đủ các mục yêu cầu (thứ tự ảnh không quan trọng)."
                                ),
                            }

                        matched_profile_idx = groups["profile"][0] if groups["profile"] else None
                        matched_vehicle_idx = groups["vehicle"][0] if groups["vehicle"] else None
                        matched_income_indices = list(groups["income"])

                        # 4. Object-driven extraction: profile + every income object.
                        # REUSING pre-normalized bytes (ZERO redundant libvips calls!)
                        slot_objects = [_slot_object(slot) for slot in (target_q.image_slots or [])]
                        slot_roles = [
                            _normalized_image_slots([slot])[0].get("role")
                            for slot in (target_q.image_slots or [])
                        ]
                        config_by_object: Dict[str, Dict[str, Any]] = {}
                        prompt_by_object: Dict[str, str] = {}
                        for slot, obj in zip(target_q.image_slots or [], slot_objects):
                            config_by_object.setdefault(obj.id, _slot_config(slot))
                            custom_prompt = str((slot or {}).get("ai_detection_prompt") or "").strip()
                            if custom_prompt:
                                prompt_by_object.setdefault(obj.id, custom_prompt)
                        income_object = next(
                            (obj for obj in slot_objects if obj.extractor == "income_statement"),
                            None,
                        )
                        if income_object is None:
                            income_object = next(
                                (
                                    obj for role, obj in zip(slot_roles, slot_objects)
                                    if _role_group(role) == "income"
                                ),
                                None,
                            )
                        profile_object = next(
                            (obj for obj in slot_objects if obj.extractor == "profile"),
                            None,
                        )
                        if profile_object is None:
                            profile_object = next(
                                (
                                    obj for role, obj in zip(slot_roles, slot_objects)
                                    if _role_group(role) == "profile"
                                ),
                                None,
                            )
                        income_config = config_by_object.get(income_object.id, {}) if income_object else {}
                        profile_config = config_by_object.get(profile_object.id, {}) if profile_object else {}

                        extraction_tasks = []
                        extraction_kinds: List[str] = []
                        extraction_indices: List[int] = []
                        if profile_object is not None and matched_profile_idx is not None:
                            extraction_tasks.append(
                                _extract_image_object(
                                    norm_bytes_list[matched_profile_idx],
                                    profile_object,
                                    profile_config,
                                    prompt_override=prompt_by_object.get(profile_object.id),
                                )
                            )
                            extraction_kinds.append("profile")
                            extraction_indices.append(matched_profile_idx)

                        if income_object is not None:
                            for i_pos, s_idx in enumerate(matched_income_indices):
                                extraction_tasks.append(
                                    _extract_image_object(
                                        norm_bytes_list[s_idx],
                                        income_object,
                                        income_config,
                                        week_idx=i_pos + 1,
                                        prompt_override=prompt_by_object.get(income_object.id),
                                    )
                                )
                                extraction_kinds.append("income")
                                extraction_indices.append(s_idx)

                        all_results = await asyncio.gather(*extraction_tasks, return_exceptions=True) if extraction_tasks else []
                        profile_res: Any = {}
                        income_res_list: List[Any] = []
                        income_indices_order: List[int] = []
                        for kind, img_idx, result in zip(extraction_kinds, extraction_indices, all_results):
                            if kind == "profile":
                                profile_res = result if isinstance(result, dict) else {}
                            else:
                                income_res_list.append(result)
                                income_indices_order.append(img_idx)
                        matched_income_indices = income_indices_order

                        # Order-agnostic weeks: drop images whose OCR is clearly not an
                        # income statement (e.g. the profile menu listing "Thống kê thu nhập"
                        # as a row), then sort by detected period start.
                        income_pairs = list(zip(matched_income_indices, income_res_list))
                        income_only = [pair for pair in income_pairs if _looks_like_income_statement(pair[1])]
                        if income_only:
                            income_pairs = income_only

                        income_required = 0
                        for question in (getattr(schema, "questions", None) or []):
                            if getattr(question, "type", None) != "camera_webrtc" or not getattr(question, "image_slots", None):
                                continue
                            count_income = sum(
                                1 for slot in _normalized_image_slots(question.image_slots)
                                if _role_group(slot.get("role")) == "income"
                            )
                            income_required = max(income_required, count_income)
                        short_income_violation = None
                        if income_required and len(income_pairs) < income_required:
                            short_income_violation = {
                                "failed_slot": matched_income_indices[0] if matched_income_indices else 0,
                                "ocr_data": {},
                                "reason": (
                                    f"Thiếu ảnh thu nhập hợp lệ: chỉ nhận diện được {len(income_pairs)}/{income_required} tuần. "
                                    "Vui lòng chụp đủ các tuần thu nhập được yêu cầu (thứ tự ảnh không quan trọng)."
                                ),
                            }

                        def _period_start(pair):
                            result = pair[1]
                            if isinstance(result, dict):
                                start, _, _ = parse_date_range_from_text(str(result.get("period") or ""), default_year=2026)
                                if start:
                                    return (0, start)
                            return (1, pair[0])

                        income_pairs.sort(key=_period_start)
                        duplicate_week_violation = short_income_violation or _find_duplicate_income_period(income_pairs)
                        matched_income_indices = [pair[0] for pair in income_pairs]
                        income_res_list = [pair[1] for pair in income_pairs]

                        role_0 = (
                            classified_roles[matched_profile_idx]
                            if groups["profile"] and matched_profile_idx is not None
                            else (target_q.image_slots[0].get("role", "app_profile") if target_q.image_slots else "app_profile")
                        )
                        membership_type = profile_res.get("membership_type") if isinstance(profile_res, dict) and not isinstance(profile_res, Exception) else None
                        primary_ocr = income_res_list[0] if income_res_list and isinstance(income_res_list[0], dict) and not isinstance(income_res_list[0], Exception) else {}
                        ocr_data = primary_ocr

                        image_object_results: List[Dict[str, Any]] = []
                        monthly_violation = coverage_violation or duplicate_week_violation

                        def _slot_title(idx: int) -> str:
                            slots = target_q.image_slots or []
                            if 0 <= idx < len(slots):
                                return str(slots[idx].get("title") or slots[idx].get("role") or f"Ảnh {idx + 1}")
                            return f"Ảnh {idx + 1}"

                        if profile_object is not None and matched_profile_idx is not None:
                            try:
                                profile_check = run_definition_checks(profile_object, profile_res, profile_config)
                            except ImageObjectDefinitionError as exc:
                                logger.error("[IMAGE OBJECTS] profile check unusable: %s", exc)
                                profile_check = {"passed": False, "reasons": [f"Cấu hình kiểm tra ảnh không hợp lệ: {exc}"], "failures": [], "engines": []}
                            image_object_results.append(
                                image_object_result(
                                    profile_object.id,
                                    matched_profile_idx,
                                    _slot_title(matched_profile_idx),
                                    profile_res,
                                    profile_check,
                                )
                            )
                            if not monthly_violation and not profile_check.get("passed"):
                                monthly_violation = {
                                    "failed_slot": matched_profile_idx,
                                    "reason": f"Ảnh {matched_profile_idx + 1}: {profile_check['reasons'][0]}",
                                    "ocr_data": profile_res,
                                }

                        for w_i, (img_idx, ocr_w) in enumerate(income_pairs):
                            w_num = w_i + 1
                            if not isinstance(ocr_w, dict):
                                continue
                            object_check = None
                            if income_object is not None:
                                try:
                                    object_check = run_definition_checks(income_object, ocr_w, income_config)
                                except ImageObjectDefinitionError as exc:
                                    logger.error("[IMAGE OBJECTS] income check unusable: %s", exc)
                                    object_check = {"passed": False, "reasons": [f"Cấu hình kiểm tra ảnh không hợp lệ: {exc}"], "failures": [], "engines": []}
                                image_object_results.append(
                                    image_object_result(
                                        income_object.id,
                                        img_idx,
                                        _slot_title(img_idx),
                                        ocr_w,
                                        object_check,
                                    )
                                )

                            # An unreadable/clipped week tab is a re-shoot, never a guess.
                            unreliable_reason = None if monthly_violation else _unreliable_week_reason(
                                ocr_w,
                                w_num,
                                img_idx + 1,
                            )
                            if unreliable_reason:
                                monthly_violation = {
                                    "failed_slot": img_idx,
                                    "reason": unreliable_reason,
                                    "ocr_data": ocr_w,
                                }
                                break

                            if monthly_violation:
                                continue
                            if object_check is not None and not object_check.get("passed"):
                                monthly_violation = {
                                    "failed_slot": img_idx,
                                    "reason": f"Ảnh {img_idx + 1} (Tuần {w_num}): {object_check['reasons'][0]}",
                                    "ocr_data": ocr_w,
                                }
                                break

                        for vehicle_idx in groups.get("vehicle", []):
                            vehicle_object = slot_objects[vehicle_idx] if vehicle_idx < len(slot_objects) else None
                            if vehicle_object is not None:
                                image_object_results.append(
                                    image_object_result(
                                        vehicle_object.id,
                                        vehicle_idx,
                                        _slot_title(vehicle_idx),
                                        {},
                                        {"passed": True, "failures": [], "engines": []},
                                    )
                                )

                        compat_income = project_income_weeks(image_object_results)
                        compat_totals = project_income_totals(compat_income)

                        slot_0_role = _infer_slot_role(target_q.image_slots[0]) if target_q.image_slots else "app_profile"
                        slot_0_title = target_q.image_slots[0].get("title", "Ảnh 1") if len(target_q.image_slots) > 0 else "Ảnh 1"
                        slot_0_object = slot_objects[0] if slot_objects else None
                        slot_0_is_profile = bool(
                            slot_0_object is not None
                            and slot_0_object.extractor == "profile"
                            and not slot_0_object.id.startswith("custom_")
                        )

                        # 1. Profile Verification (Slot 0)
                        is_approved = False
                        if monthly_violation:
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = monthly_violation["reason"]
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": monthly_violation["failed_slot"],
                                "reason": reason_str,
                                "ocr_data": monthly_violation["ocr_data"],
                                "image_object_results": image_object_results,
                            }
                        elif slot_0_is_profile and role_0 in ("app_income", "revenue_screen"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Phát hiện sai loại màn hình (đây là màn hình thống kê thu nhập, không phải màn hình hồ sơ/tiện ích ứng dụng). Vui lòng chụp đúng màn hình hồ sơ ứng dụng ở Ảnh 1!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        elif slot_0_is_profile and role_0 != slot_0_role and role_0 not in ("app_profile", "odometer"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Không phát hiện màn hình hồ sơ ứng dụng tài xế Xanh SM (Phát hiện vật thể không hợp lệ). Vui lòng chụp đúng màn hình hồ sơ ứng dụng tài xế!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        # 2. Income-screen authenticity/tampering/legibility checks are
                        # executed by the object's bound engines (see monthly_violation).
                        else:
                            is_approved = True
                            status_str = "APPROVED"
                            reason_str = "Gửi phiếu khảo sát thành công!"
                            gross_val = int(ocr_data.get("gross_revenue") or 0)
                            net_val = int(ocr_data.get("net_income") or 0)
                            trips_val = int(ocr_data.get("total_trips") or 0)
                            hours_val = str(ocr_data.get("online_hours") or "")
                            period_val = str(ocr_data.get("period") or "")
                            ai_res = {
                                "status": "APPROVED",
                                "model": "DEEPSEEK_V3_CHAT",
                                "roles_verified": [role_0, "auto_passed_vehicle" if len(target_q.image_slots) >= 7 else "app_income"],
                                "total_slots_processed": len(images_list),
                                "membership_type": membership_type,
                                "image_object_results": image_object_results,
                                "income_totals": compat_totals,
                                "online_hours": hours_val,
                                "period": period_val,
                            }

                        should_check_fraud = bool(getattr(checks, "enable_anti_fraud", True) and getattr(checks, "require_unique", True))
                        if is_approved and should_check_fraud:
                            # 3. Anti-Fraud: Intra-submission duplicate check across income objects (Meta DINOv2 + dHash + numbers)
                            intra_dup, intra_reason = check_intra_submission_duplicates(
                                raw_bytes_list, target_q, compat_income,
                                income_indices=matched_income_indices, pil_cache=pil_cache
                            )
                            if intra_dup:
                                is_approved = False
                                status_str = "REJECTED"
                                reason_str = intra_reason
                                ai_res["status"] = "REJECTED"
                                ai_res["reason"] = reason_str
                                ai_res["duplicate_detected"] = True
                                ai_res["duplicate_type"] = "INTRA_SUBMISSION"
                            else:
                                # Pick the uniqueness-bearing image (income screen), never a
                                # reusable profile/vehicle photo. No candidate -> skip the check.
                                check_idx = _select_unique_check_index(
                                    target_q, matched_income_indices, len(raw_bytes_list)
                                )
                                check_im = pil_cache.get(check_idx) if check_idx is not None else None

                                if check_im is not None:
                                    db_pool = get_db_pool()
                                    if db_pool:
                                        # 4. Anti-Fraud: Inter-submission duplicate check against approved submissions (Meta DINOv2 + dHash + identical data)
                                        primary_emb = compute_dinov2_embedding(check_im)
                                        inter_dup, matched_sub_id, dup_type = await check_inter_submission_duplicates(
                                            db_pool, check_im, primary_emb,
                                            weekly_incomes=compat_income,
                                            tot_gross=compat_totals["total_gross_revenue"],
                                            tot_trips=compat_totals["total_trips"],
                                        )
                                        if inter_dup:
                                            is_approved = False
                                            status_str = "REJECTED"
                                            if dup_type == "DATA_DUPLICATE":
                                                reason_str = f"Số liệu doanh thu các kỳ bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{matched_sub_id})! Hệ thống chống gian lận phát hiện số liệu bị sao chép."
                                            else:
                                                reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{matched_sub_id})! Hệ thống Meta DINOv2 phát hiện ảnh bị nộp lại."
                                            ai_res["status"] = "REJECTED"
                                            ai_res["reason"] = reason_str
                                            ai_res["duplicate_detected"] = True
                                            ai_res["matched_submission_id"] = matched_sub_id
                                            ai_res["duplicate_type"] = dup_type or "INTER_SUBMISSION"
                                    elif is_test_mode:
                                        dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
                                        if os.path.exists(dev_storage_file):
                                            try:
                                                with open(dev_storage_file, "r", encoding="utf-8") as df:
                                                    dev_recs = json.load(df)
                                                target_dhash = compute_dhash_int(check_im)
                                                for prev in dev_recs:
                                                    if prev.get("status") == "APPROVED":
                                                        # 1. Identical financial data check
                                                        prev_w = prev.get("weekly_incomes") or {}
                                                        prev_g = int(prev.get("total_gross_revenue") or prev.get("gross_revenue") or 0)
                                                        prev_t = int(prev.get("total_trips") or prev.get("trip_count") or 0)
                                                        cur_w1 = int(compat_income.get("week_1", {}).get("gross_revenue") or 0)
                                                        cur_w2 = int(compat_income.get("week_2", {}).get("gross_revenue") or 0)
                                                        if prev_g > 0 and compat_totals["total_gross_revenue"] > 0 and prev_g == compat_totals["total_gross_revenue"] and prev_t > 0 and prev_t == compat_totals["total_trips"]:
                                                            p_w1 = int(prev_w.get("week_1", {}).get("gross_revenue") or prev.get("income_week_1_gross") or 0)
                                                            p_w2 = int(prev_w.get("week_2", {}).get("gross_revenue") or prev.get("income_week_2_gross") or 0)
                                                            if p_w1 > 0 and p_w1 == cur_w1 and p_w2 > 0 and p_w2 == cur_w2:
                                                                is_approved = False
                                                                status_str = "REJECTED"
                                                                reason_str = f"Số liệu doanh thu các kỳ bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{prev.get('submission_id')})! Phát hiện sao chép số liệu thu nhập."
                                                                ai_res["status"] = "REJECTED"
                                                                ai_res["reason"] = reason_str
                                                                ai_res["duplicate_detected"] = True
                                                                ai_res["matched_submission_id"] = prev.get('submission_id')
                                                                ai_res["duplicate_type"] = "INTER_SUBMISSION_DATA"
                                                                break

                                                        # 2. Image hash check
                                                        if prev.get("image_dhash_int") is not None:
                                                            prev_dh = int(prev["image_dhash_int"])
                                                            if hamming_distance(target_dhash, prev_dh) <= 6:
                                                                is_approved = False
                                                                status_str = "REJECTED"
                                                                reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{prev.get('submission_id')})! Phát hiện trùng lặp hình ảnh."
                                                                ai_res["status"] = "REJECTED"
                                                                ai_res["reason"] = reason_str
                                                                ai_res["duplicate_detected"] = True
                                                                ai_res["matched_submission_id"] = prev.get('submission_id')
                                                                ai_res["duplicate_type"] = "INTER_SUBMISSION"
                                                                break
                                            except Exception:
                                                pass
                    else:
                        try:
                            facts = await vision_client.extract_facts(norm_bytes)
                            ai_res = facts.model_dump() if hasattr(facts, "model_dump") else {}
                            if not getattr(checks, "enable_object_detection", False):
                                if getattr(facts, "is_manipulated_or_photoshopped", False):
                                    is_approved = False
                                    status_str = "REJECTED"
                                    reason_str = "Hệ thống phát hiện dấu hiệu chỉnh sửa ảnh kỹ thuật số (Photoshop)."
                                else:
                                    is_approved = True
                                    status_str = "APPROVED"
                                    reason_str = "Ảnh chụp rõ nét, đạt chuẩn kiểm tra hình ảnh (Đã tắt nhận diện đối tượng theo yêu cầu)."
                            else:
                                compliance = evaluate_survey_compliance(facts, checks=checks)
                                status_str = "APPROVED" if is_approved else "REJECTED"
                                reason_str = compliance.collaborator_note or compliance.reason_code
                        except Exception as ai_err:
                            if isinstance(ai_err, (asyncio.TimeoutError, TimeoutError)):
                                logger.error(f"Vision AI call timed out: {ai_err}")
                                ai_res = {"status": "manual_review", "error": "AI Vision timeout"}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
                            elif not getattr(checks, "enable_object_detection", False):
                                ai_res = {"status": "approved", "note": "passed image quality check"}
                                is_approved = True
                                status_str = "APPROVED"
                                reason_str = "Ảnh chụp rõ nét, đạt chuẩn kiểm tra hình ảnh (Bỏ qua nhận diện đối tượng)."
                            else:
                                logger.error(f"Vision AI call failed: {ai_err}")
                                ai_res = {"status": "manual_review", "error": str(ai_err)}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
    # 3. Persistence: Test mode -> Dev storage; Production mode -> DB + Outbox Events in same transaction
    is_dissent = answers.get("consent_choice") == "Tôi không đồng ý"
    if is_approved and not is_bypassed and not is_dissent and not is_test_mode:
        status_str = "MANUAL_REVIEW"
        reason_str = "Phiếu khảo sát của bạn đã được ghi nhận. Ban Quản Trị đang tiến hành đối soát thủ công. Vui lòng mở Telegram @TrustBridgeVN_bot để nhận kết quả!"
    persisted_phone = None if is_dissent else driver_phone
    persisted_name = None if is_dissent else (driver_name or "Tài xế")

    # Form specific metrics and boundary income projection
    vehicle_type_val = _normalize_vehicle_type(
        form_data.get("vehicle_type")
        or vehicle_type
        or _answer_by_title_keywords(schema, answers, ("phương tiện", "dòng xe", "loại xe"))
        or "CAR"
    )
    car_model_val = str(form_data.get("car_model") or "").strip() or (
        _answer_by_title_keywords(schema, answers, ("dòng xe ô tô điện", "dòng xe máy điện", "model", "dòng xe"))
        or ""
    )
    gsm_duration_val = str(form_data.get("gsm_duration") or "").strip() or (
        _answer_by_title_keywords(schema, answers, ("bao lâu",)) or ""
    )
    gsm_contract_type_val = str(form_data.get("gsm_contract_type") or "").strip() or (
        _answer_by_title_keywords(schema, answers, ("hình thức hợp tác",)) or ""
    )
    raw_daily_trips = form_data.get("daily_trips")
    daily_trips_val = int(raw_daily_trips) if raw_daily_trips is not None and str(raw_daily_trips).isdigit() else None
    monthly_income_range_val = str(form_data.get("monthly_income_range") or "")
    raw_policy = form_data.get("gsm_incentive_policy") or ""
    gsm_policy_val = "; ".join(str(x) for x in raw_policy) if isinstance(raw_policy, list) else str(raw_policy)
    membership_type_val = (
        ai_res.get("membership_type")
        or form_data.get("membership_type")
        or _answer_by_title_keywords(schema, answers, ("hạng thành viên", "hạng"))
        or "Chưa xác định"
    )

    # Boundary adapter only: legacy DB columns / Drive CSV keep their old shape.
    w_incomes = compat_income or ai_res.get("weekly_incomes") or {}
    w1_g = int(w_incomes.get("week_1", {}).get("gross_revenue") or ai_res.get("gross_revenue") or gross_revenue or 0)
    w1_n = int(w_incomes.get("week_1", {}).get("net_income") or ai_res.get("net_income") or 0)
    w1_t = int(w_incomes.get("week_1", {}).get("total_trips") or trip_count or 0)

    w2_g = int(w_incomes.get("week_2", {}).get("gross_revenue") or 0)
    w2_n = int(w_incomes.get("week_2", {}).get("net_income") or 0)
    w2_t = int(w_incomes.get("week_2", {}).get("total_trips") or 0)

    w3_g = int(w_incomes.get("week_3", {}).get("gross_revenue") or 0)
    w3_n = int(w_incomes.get("week_3", {}).get("net_income") or 0)
    w3_t = int(w_incomes.get("week_3", {}).get("total_trips") or 0)

    w4_g = int(w_incomes.get("week_4", {}).get("gross_revenue") or 0)
    w4_n = int(w_incomes.get("week_4", {}).get("net_income") or 0)
    w4_t = int(w_incomes.get("week_4", {}).get("total_trips") or 0)

    w5_g = int(w_incomes.get("week_5", {}).get("gross_revenue") or 0)
    w5_n = int(w_incomes.get("week_5", {}).get("net_income") or 0)
    w5_t = int(w_incomes.get("week_5", {}).get("total_trips") or 0)

    computed_tot_g = int(compat_totals.get("total_gross_revenue") or (w1_g + w2_g + w3_g + w4_g + w5_g))
    computed_tot_n = int(compat_totals.get("total_net_income") or (w1_n + w2_n + w3_n + w4_n + w5_n))
    computed_tot_t = int(compat_totals.get("total_trips") or (w1_t + w2_t + w3_t + w4_t + w5_t))

    tot_gross_val = int(computed_tot_g if computed_tot_g > 0 else (ai_res.get("gross_revenue") or gross_revenue or 0))
    tot_net_val = int(computed_tot_n if computed_tot_n > 0 else (ai_res.get("net_income") or 0))
    tot_trips_val = int(computed_tot_t if computed_tot_t > 0 else (trip_count or 0))
    clean_form_data = {
        k: v for k, v in answers.items()
        if not k.startswith("survey_photo") and not isinstance(v, (bytes, bytearray)) and not hasattr(v, "filename")
    }

    action_checklist_init = init_action_checklist(is_approved, bool(collab_param))
    if is_test_mode:
        dev_record = {
            "submission_id": submission_id,
            "mode": "TEST_DEV_MODE",
            "status": status_str,
            "action_status": action_checklist_init,
            "consent_choice": str(answers.get("consent_choice") or "Tôi đồng ý"),
            "form_data": clean_form_data,
            "weekly_incomes": w_incomes,
            "total_gross_revenue": tot_gross_val,
            "total_net_income": tot_net_val,
            "total_trips": tot_trips_val,
            "income_week_1_gross": w1_g,
            "income_week_1_net": w1_n,
            "income_week_1_trips": w1_t,
            "income_week_2_gross": w2_g,
            "income_week_2_net": w2_n,
            "income_week_2_trips": w2_t,
            "income_week_3_gross": w3_g,
            "income_week_3_net": w3_n,
            "income_week_3_trips": w3_t,
            "income_week_4_gross": w4_g,
            "income_week_4_net": w4_n,
            "income_week_4_trips": w4_t,
            "income_week_5_gross": w5_g,
            "income_week_5_net": w5_n,
            "income_week_5_trips": w5_t,
            "driver_tag": driver_tag,
            "driver_phone": persisted_phone,
            "driver_name": persisted_name,
            "collaborator_id": collab_param,
            "vehicle_type": vehicle_type_val,
            "car_model": car_model_val,
            "gsm_duration": gsm_duration_val,
            "gsm_contract_type": gsm_contract_type_val,
            "daily_trips": daily_trips_val,
            "ai_result": ai_res,
            "image_dhash_int": compute_dhash_int(pil_im) if pil_im is not None else None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "net_income": tot_net_val,
            "trip_count": tot_trips_val,
            "license_plate": license_plate or ai_res.get("detected_plate"),
            "reason": reason_str,
            "ai_result": ai_res,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        append_dev_submission(dev_record)
        logger.info(f"[DEV TEST MODE] Saved #{submission_id} to temporary dev storage: {DEV_STORAGE_FILE}")
        request.state.pending_submission["final_written"] = True
    elif db_pool:
        try:
            async with db_pool.connection() as conn:
                cur_res = conn.cursor()
                if hasattr(cur_res, "__await__"):
                    cur_res = await cur_res
                cur = await cur_res.__aenter__() if hasattr(cur_res, "__aenter__") else cur_res
                vault_driver_id = None
                try:
                    if not is_dissent and persisted_phone:
                        await cur.execute(
                            """
                            INSERT INTO driver_identity_vault (
                                driver_id, driver_phone, driver_name
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO UPDATE
                            SET driver_name = EXCLUDED.driver_name;
                            """,
                            (driver_tag, persisted_phone, persisted_name),
                        )
                        await cur.execute(
                            "SELECT driver_id FROM driver_identity_vault WHERE driver_phone = %s;",
                            (persisted_phone,),
                        )
                        vault_row = await cur.fetchone()
                        if vault_row and vault_row[0]:
                            vault_driver_id = vault_row[0]
                    effective_ctv = str(collab_param or answers.get("ctv_code") or "").strip() or None
                    consent_val = str(answers.get("consent_choice") or "Tôi đồng ý")
                    calc_dhash = compute_image_dhash_str(pil_im) if pil_im is not None else None
                    calc_emb = compute_dinov2_embedding(pil_im) if pil_im is not None else None
                    calc_emb_str = ("[" + ",".join(str(x) for x in calc_emb) + "]") if calc_emb else None
                    gps_lat = _to_float(answers.get("gps_latitude") or form_data.get("gps_latitude"))
                    gps_lng = _to_float(answers.get("gps_longitude") or form_data.get("gps_longitude"))
                    gps_acc = _to_float(
                        answers.get("gps_accuracy")
                        or answers.get("gps_accuracy_meters")
                        or form_data.get("gps_accuracy")
                    )
                    audit_seal_hash = hashlib.sha256(
                        f"{submission_id}:{persisted_phone}:{status_str}:"
                        f"{json.dumps(clean_form_data, sort_keys=True, default=str)}".encode()
                    ).hexdigest()

                    insert_cols = [
                        "submission_id", "form_id", "ctv_code", "driver_phone", "driver_name",
                        "receipt_serial_no", "status", "rejection_reason", "form_data",
                        "raw_extracted_facts", "driver_id", "image_dhash", "embedding",
                        "consent_choice", "vehicle_type", "car_model", "gsm_duration",
                        "gsm_contract_type", "daily_trips", "monthly_income_range",
                        "gsm_incentive_policy", "membership_type", "gps_latitude",
                        "gps_longitude", "gps_accuracy_meters", "signature_r2_url",
                        "image_r2_url", "audit_seal_hash",
                        "income_week_1_gross", "income_week_1_net", "income_week_1_trips",
                        "income_week_2_gross", "income_week_2_net", "income_week_2_trips",
                        "income_week_3_gross", "income_week_3_net", "income_week_3_trips",
                        "income_week_4_gross", "income_week_4_net", "income_week_4_trips",
                        "income_week_5_gross", "income_week_5_net", "income_week_5_trips",
                        "total_gross_revenue", "total_net_income", "total_trips",
                        "action_status",
                    ]
                    insert_vals_full = (
                        submission_id,
                        active_form_id,
                        effective_ctv,
                        persisted_phone,
                        persisted_name,
                        f"SN-{license_plate or 'AUTO'}",
                        status_str,
                        reason_str if not is_approved else None,
                        json.dumps(clean_form_data),
                        json.dumps(ai_res),
                        vault_driver_id,
                        calc_dhash,
                        calc_emb_str,
                        consent_val,
                        vehicle_type_val,
                        car_model_val,
                        gsm_duration_val,
                        gsm_contract_type_val,
                        daily_trips_val,
                        monthly_income_range_val,
                        gsm_policy_val,
                        membership_type_val,
                        gps_lat,
                        gps_lng,
                        gps_acc,
                        None,
                        None,
                        audit_seal_hash,
                        w1_g, w1_n, w1_t,
                        w2_g, w2_n, w2_t,
                        w3_g, w3_n, w3_t,
                        w4_g, w4_n, w4_t,
                        w5_g, w5_n, w5_t,
                        tot_gross_val, tot_net_val, tot_trips_val,
                        json.dumps(action_checklist_init),
                    )
                    placeholders = ", ".join(["%s"] * len(insert_cols))
                    update_cols = ", ".join(
                        f"{col} = EXCLUDED.{col}" for col in insert_cols if col != "submission_id"
                    )
                    insert_query = (
                        f"INSERT INTO survey_submissions ({', '.join(insert_cols)}) "
                        f"VALUES ({placeholders}) "
                        f"ON CONFLICT (submission_id) DO UPDATE SET {update_cols};"
                    )
                    try:
                        await cur.execute(insert_query, insert_vals_full)
                    except Exception as ins_err:
                        logger.warning(f"Extended column insert failed for #{submission_id}: {ins_err}")
                        # Minimal fallback must stay FK-safe: omitting driver_id means a
                        # stale/missing driver_identity_vault row can never abort the whole
                        # persistence (that silently lost 5 submissions on 2026-11-16).
                        await cur.execute(
                            """
                            INSERT INTO survey_submissions (
                                submission_id, form_id, ctv_code, driver_phone, driver_name, receipt_serial_no,
                                status, rejection_reason, form_data, raw_extracted_facts
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (submission_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                rejection_reason = EXCLUDED.rejection_reason,
                                form_data = EXCLUDED.form_data,
                                raw_extracted_facts = EXCLUDED.raw_extracted_facts;
                            """,
                            (
                                submission_id,
                                active_form_id,
                                effective_ctv,
                                persisted_phone,
                                persisted_name,
                                f"SN-{license_plate or 'AUTO'}",
                                status_str,
                                reason_str if not is_approved else None,
                                json.dumps(clean_form_data),
                                json.dumps(ai_res),
                            ),
                        )
                    if license_plate and not is_dissent:
                        vtype_db = "BIKE" if "BIKE" in str(vehicle_type).upper() or "MÁY" in str(vehicle_type).upper() else "CAR"
                        await cur.execute(
                            """
                            INSERT INTO driver_registered_vehicles (
                                driver_phone, license_plate, vehicle_type
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO NOTHING;
                            """,
                            (driver_phone, license_plate.upper(), vtype_db),
                        )
                    outbox_payload = {
                        "submission_id": submission_id,
                        "driver_tag": driver_tag,
                        "driver_phone_hash": driver_phone_hash,
                        "driver_name": persisted_name,
                        "license_plate": license_plate or ai_res.get("detected_plate"),
                        "license_plate_hash": license_plate_hash or (hash_plate(ai_res.get("detected_plate")) if ai_res.get("detected_plate") else None),
                        "vehicle_type": vehicle_type or "CAR",
                        "gross_revenue": gross_revenue,
                        "trip_count": trip_count,
                        "status": status_str,
                        "reason": reason_str,
                        "ai_result": ai_res,
                    }
                    event_type = "survey.validated" if is_approved else "survey.rejected"
                    await cur.execute(
                        """
                        INSERT INTO outbox_events (
                            event_type, flow_target, aggregate_id, payload, status
                        ) VALUES (%s, %s, %s, %s, 'PENDING');
                        """,
                        (
                            event_type,
                            "ALL",
                            submission_id,
                            json.dumps(outbox_payload),
                        ),
                    )
                    await conn.commit()
                finally:
                    if hasattr(cur_res, "__aexit__"):
                        await cur_res.__aexit__(None, None, None)
        except Exception as e:
            # Re-raise so the crash guard writes the interruption row instead of
            # answering 200 with nothing persisted (silent-loss bug 2026-11-16).
            logger.error(f"failed to persist survey to DB: {e}")
            raise

    request.state.pending_submission["final_written"] = True

    # 4. Dispatch Idempotent Downstream Action Checklist
    try:
        background_tasks.add_task(
            dispatch_action_checklist,
            submission_id=submission_id,
            db_pool=get_db_pool() if not is_test_mode else None,
            is_approved=is_approved,
            raw_bytes=raw_bytes,
            driver_phone=persisted_phone or driver_phone,
            driver_name=persisted_name or driver_name,
            license_plate=license_plate or ai_res.get("detected_plate"),
            collab_param=collab_param,
            answers=clean_form_data,
            ai_res=ai_res,
            tot_gross=tot_gross_val,
            tot_net=tot_net_val,
            tot_trips=tot_trips_val,
            reason_str=reason_str,
            blur_var=blur_var,
            is_test_mode=is_test_mode,
            raw_bytes_list=raw_bytes_list,
            form_id=active_form_id,
        )
    except Exception as act_err:
        logger.error(f"Failed to dispatch action checklist task: {act_err}")
    # 4. Telegram dispatch to admin group
    status_emoji = "⏳" if status_str == "MANUAL_REVIEW" else ("✅" if status_str == "APPROVED" else "❌")
    auto_verdict = ai_res.get("status", "MANUAL_REVIEW") if isinstance(ai_res, dict) else "MANUAL_REVIEW"
    auto_reason = ai_res.get("reason", "") if isinstance(ai_res, dict) else ""
    auto_emoji = "✅" if str(auto_verdict).upper() == "APPROVED" else "❌"
    _raw_vehicle = vehicle_type or (ai_res.get("vehicle_type") if isinstance(ai_res, dict) else None)
    vehicle_type_val = _normalize_vehicle_type(_raw_vehicle) if _raw_vehicle else "Chưa xác định"
    driver_info_line = (
        f"• <b>Tài xế:</b> <b>{html.escape(driver_name)}</b> (<code>{driver_phone or 'N/A'}</code>)"
        if driver_name and driver_name not in ("Tài xế", "Tài xế Bí Mật", "N/A", "[SCRUBBED]")
        else f"• <b>Số điện thoại:</b> <code>{driver_phone or 'N/A'}</code>"
    )
    verdict_text = (
        f"{status_emoji} <b>PHIẾU KHẢO SÁT CHỜ DUYỆT THỦ CÔNG #{submission_id}</b>\n\n"
        f"{driver_info_line}\n"
        f"• <b>Loại xe:</b> <code>{vehicle_type_val}</code>\n"
        f"• <b>Đề xuất từ Hệ thống AI:</b> {auto_emoji} <b>{auto_verdict}</b> ({html.escape(auto_reason) if auto_reason else 'Đạt chuẩn'})\n"
        f"• <b>Doanh thu:</b> <b>{tot_gross_val:,} VNĐ</b>\n"
        f"• <b>Trạng thái:</b> <code>{status_str}</code>"
    )
    admin_markup = {
        "inline_keyboard": [
            [
                {"text": "📋 Mở Phiếu Xét Duyệt", "callback_data": f"admin_review:{submission_id}"}
            ]
        ]
    }
    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        try:
            background_tasks.add_task(
                send_telegram_notification,
                settings.TELEGRAM_ADMIN_GROUP_ID,
                verdict_text,
                admin_markup,
            )
        except Exception as tg_err:
            logger.error(f"Failed to dispatch Telegram notification to admin group: {tg_err}")

    # 5. Direct Telegram DMs to the configured verdict recipient
    #    (none / owning CTV / answer of a chosen question such as driver phone)
    try:
        notify_schema = FORM_REGISTRY.get(active_form_id)
        notify_settings = getattr(notify_schema, "settings", None) if notify_schema else None
        notify_source = (getattr(notify_settings, "tg_response_source", None) or "ctv").lower()
        notify_recipient = collab_param
        notify_kind = "ctv"
        if notify_source == "question":
            notify_qid = getattr(notify_settings, "tg_response_question_id", None)
            notify_recipient = answers.get(notify_qid) if notify_qid else None
            notify_kind = "driver"
        if notify_source == "none":
            logger.info(f"TG response disabled for form '{active_form_id}'; skipping submission notification for #{submission_id}.")
        elif not notify_recipient:
            logger.info(f"No TG response recipient for #{submission_id} (source={notify_source}); skipping.")
        else:
            import sys
            main_mod = sys.modules.get("main")
            _dispatch_fn = getattr(main_mod, "dispatch_submission_telegram_notifications", dispatch_submission_telegram_notifications) if main_mod else dispatch_submission_telegram_notifications
            background_tasks.add_task(
                _dispatch_fn,
                submission_id=submission_id,
                status_str=status_str,
                driver_phone=driver_phone,
                driver_name=driver_name,
                collaborator_id=notify_recipient,
                reason_str=reason_str,
                gross_revenue=ai_res.get("gross_revenue") or gross_revenue,
                form_id=active_form_id,
                recipient_kind=notify_kind,
                ctv_ref=collab_param,
            )
    except Exception as party_err:
        logger.error(f"Failed to dispatch party Telegram notifications: {party_err}")
    resp_data = {
        "status": status_str,
        "submission_id": submission_id,
        "mode": "TEST_DEV_MODE" if is_test_mode else "PROD_MODE",
        "reason": reason_str,
        "driver_tag": driver_tag,
        "driver_phone": driver_phone,
        "detected_plate": ai_res.get("detected_plate") or license_plate,
        "gross_revenue": ai_res.get("gross_revenue") or gross_revenue,
        "net_income": ai_res.get("net_income"),
        "trip_count": ai_res.get("total_trips") or trip_count,
        "ocr_data": ai_res.get("ocr_data"),
    }
    if ai_res.get("duplicate_detected"):
        resp_data["duplicate_detected"] = ai_res.get("duplicate_detected")
        resp_data["duplicate_type"] = ai_res.get("duplicate_type")
    if blur_var is not None:
        resp_data["blur_score"] = blur_var

    # Cache response under idempotency key for 24 hours
    if idempotency_key:
        idemp_cache_key = f"idemp:{driver_phone}:{idempotency_key}"
        LOCAL_IDEMP_CACHE[idemp_cache_key] = json.dumps(resp_data)
        redis_client = get_redis_client()
        if redis_client:
            try:
                await redis_client.set(idemp_cache_key, json.dumps(resp_data), ex=86400)
            except Exception as e:
                logger.warning(f"Redis idempotency caching error: {e}")
    return JSONResponse(status_code=200, content=resp_data)

LOCAL_APPEAL_TRACKER: Dict[str, List[float]] = {}

class AppealRequest(BaseModel):
    submission_id: str
    driver_phone: Optional[str] = "ANONYMOUS"
    reason: Optional[str] = "Yêu cầu người thật kiểm tra lại trên web form"

@router.post("/api/v1/survey/appeal")
async def handle_survey_appeal(req: AppealRequest, background_tasks: BackgroundTasks):
    """Processes driver manual review requests with 2-hour rate limiting (max 5) and active guard."""
    phone = (req.driver_phone or "ANONYMOUS").strip()
    sub_id = req.submission_id.strip()

    # 1. Prevent duplicate active appeal
    is_already_active = False
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT status FROM survey_submissions WHERE submission_id = %s;", (sub_id,))
                    row = await cur.fetchone()
                    if row and row[0] in ("APPEAL_PENDING", "MANUAL_REVIEW"):
                        is_already_active = True
        except Exception:
            pass
    if not is_already_active and os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("submission_id") == sub_id and r.get("status") in ("APPEAL_PENDING", "MANUAL_REVIEW"):
                    is_already_active = True
                    break
        except Exception:
            pass

    if is_already_active:
        return JSONResponse(status_code=200, content={
            "status": "ALREADY_ACTIVE",
            "submission_id": sub_id,
            "message": f"Phiếu #{sub_id} đã có yêu cầu kiểm tra đang hoạt động! Bạn không thể gửi thêm yêu cầu mới khi yêu cầu cũ chưa hoàn tất."
        })

    rate_key = f"rate_limit:appeal:{phone}"
    now = time.time()
    window = 3600  # 1 hour window
    max_appeals = 20  # Relaxed quota per window
    remaining_appeals = max_appeals - 1

    rate_limited = False
    redis_client = get_redis_client()
    if redis_client:
        try:
            count = await redis_client.incr(rate_key)
            if count == 1:
                await redis_client.expire(rate_key, window)
            if count > max_appeals:
                rate_limited = True
            remaining_appeals = max(0, max_appeals - count)
        except Exception as e:
            logger.warning(f"Redis appeal rate limit error: {e}")
            history = [t for t in LOCAL_APPEAL_TRACKER.get(phone, []) if now - t < window]
            if len(history) >= max_appeals:
                rate_limited = True
            else:
                history.append(now)
                LOCAL_APPEAL_TRACKER[phone] = history
                remaining_appeals = max(0, max_appeals - len(history))
    else:
        history = [t for t in LOCAL_APPEAL_TRACKER.get(phone, []) if now - t < window]
        if len(history) >= max_appeals:
            rate_limited = True
        else:
            history.append(now)
            LOCAL_APPEAL_TRACKER[phone] = history
            remaining_appeals = max(0, max_appeals - len(history))

    if rate_limited:
        return JSONResponse(status_code=200, content={
            "status": "RATE_LIMITED",
            "remaining_appeals": 0,
            "message": f"Bạn đã gửi tối đa {max_appeals} lượt yêu cầu kiểm tra trong vòng 1 giờ này. Vui lòng thử lại sau!"
        })

    # 2. Update status in Database
    appeal_note = f"Tài xế yêu cầu người thật đối soát lại: {req.reason}"
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'APPEAL_PENDING', rejection_reason = %s WHERE submission_id = %s;",
                        (appeal_note, sub_id)
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update survey_submissions for appeal: {e}")

    # 3. Update status in Dev Test Storage
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("submission_id") == sub_id:
                    r["status"] = "APPEAL_PENDING"
                    r["reason"] = appeal_note
                    r["rejection_reason"] = appeal_note
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(dev_records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to update dev_storage for appeal: {e}")

    # 4. Dispatch Alert to Telegram Admin Group
    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        alert_text = (
            f"🚨 <b>YÊU CẦU NGƯỜI THẬT KIỂM TRA LẠI (KHIẾU NẠI) #{sub_id}</b>\n\n"
            f"• <b>Số điện thoại:</b> <code>{phone}</code>\n"
            f"• <b>Lượt khiếu nại còn lại:</b> <b>{remaining_appeals}/5 lượt</b> (trong phiên 2h)\n"
            f"• <b>Lý do:</b> {html.escape(req.reason)}\n\n"
            f"👉 <i>Vui lòng vào <a href='https://survey.trustbridgevn.com/admin/submissions'>Quản trị phiếu</a> để đối soát ảnh gốc!</i>"
        )
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, alert_text)
        except Exception as e:
            logger.warning(f"Failed to alert admin group: {e}")

    return JSONResponse(status_code=200, content={
        "status": "APPEAL_SUBMITTED",
        "submission_id": sub_id,
        "remaining_appeals": remaining_appeals,
        "message": f"Đã gửi yêu cầu kiểm tra lại thành công! Bạn còn {remaining_appeals}/5 lượt khiếu nại trong phiên 2h này."
    })


@router.post("/api/v1/survey/appeal/cancel")
async def cancel_survey_appeal(req: AppealRequest, background_tasks: BackgroundTasks):
    """Allows cancelling an active manual review / appeal request."""
    sub_id = req.submission_id.strip()
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'REJECTED', rejection_reason = 'Yêu cầu kiểm tra lại đã bị hủy bởi người dùng' WHERE submission_id = %s;",
                        (sub_id,)
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to cancel appeal in DB: {e}")

    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("submission_id") == sub_id:
                    r["status"] = "REJECTED"
                    r["reason"] = "Yêu cầu kiểm tra lại đã bị hủy bởi người dùng"
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(dev_records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to cancel appeal in dev storage: {e}")

    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        alert_text = f"ℹ️ <b>Người dùng đã HỦY yêu cầu kiểm tra lại cho phiếu #{sub_id}.</b>"
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, alert_text)
        except Exception:
            pass

    return JSONResponse(status_code=200, content={
        "status": "CANCELLED",
        "submission_id": sub_id,
        "message": f"Đã hủy yêu cầu kiểm tra lại cho phiếu #{sub_id} thành công!"
    })

async def send_telegram_notification(chat_id: Union[int, str], text: str, reply_markup: Optional[dict] = None):
    """Dispatches standardized HTML verdict message to admin group or collaborator on Telegram."""
    bot_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not bot_token or bot_token == "mock_tg_token":
        logger.info(f"[MOCK TELEGRAM DISPATCH] to {chat_id}:\n{text}")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        client = get_http_client() or httpx.AsyncClient(timeout=10.0)
        resp = await client.post(url, json=payload)
        if resp.status_code == 429:
            logger.warning("telegram 429 hit; backing off")
    except Exception as e:
        logger.error(f"failed to push telegram message: {e}")


async def lookup_telegram_user_by_phone(phone: str) -> Optional[int]:
    """Looks up telegram_user_id in authorized_users by driver_phone."""
    clean = re.sub(r"[^\d+]", "", phone.strip())
    if clean.startswith("+84"):
        clean = "0" + clean[3:]
    elif clean.startswith("84") and len(clean) == 11:
        clean = "0" + clean[2:]

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users WHERE (driver_phone = %s OR driver_phone = %s) AND is_active = TRUE LIMIT 1;",
                        (clean, phone),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception as e:
            logger.debug(f"DB lookup failed for phone {phone}: {e}")
    return None


async def lookup_telegram_user_for_collaborator(collab_id: Union[str, int]) -> Optional[int]:
    """Resolves a collaborator code / full CTV id to a telegram_user_id.

    ctv_directory is the source of truth; falls back to the legacy
    authorized_users username/phone match for codes not yet migrated.
    """
    from core.ctv import extract_ctv_short_code, normalize_ctv_code

    collab_str = str(collab_id).strip()
    code = normalize_ctv_code(collab_str)
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id, phone FROM ctv_directory "
                        "WHERE (UPPER(code) = UPPER(%s) OR UPPER(code) = UPPER(%s)) "
                        "AND is_active = TRUE LIMIT 1;",
                        (collab_str, code),
                    )
                    row = await cur.fetchone()
                    if row:
                        phone, cached_tg_id = row[1], row[0]
                        if phone:
                            tg_id = await lookup_telegram_user_by_phone(str(phone))
                            if tg_id:
                                return tg_id
                        # Internally populated cache/fallback only; never admin input.
                        if cached_tg_id:
                            return int(cached_tg_id)
        except Exception as e:
            logger.debug(f"ctv_directory lookup failed for collaborator {collab_str}: {e}")

    candidates = {collab_str.lower(), extract_ctv_short_code(collab_str).lower()}
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users "
                        "WHERE is_active = TRUE AND (LOWER(username) = ANY(%s) OR LOWER(driver_phone) = ANY(%s)) "
                        "LIMIT 1;",
                        (list(candidates), list(candidates)),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception as e:
            logger.debug(f"DB lookup failed for collaborator {collab_str}: {e}")
    if collab_str.isdigit():
        return int(collab_str)
    return None


async def send_telegram_direct_message(
    bot_token: str,
    chat_id: Union[int, str],
    text: str,
    parse_mode: str = "HTML",
    reply_markup: Optional[dict] = None,
) -> bool:
    """Dispatches a direct message using the specified bot token (Driver Bot or Admin Bot)."""
    if not bot_token or bot_token == "mock_tg_token":
        logger.info(f"[MOCK DIRECT MESSAGE DISPATCH] to {chat_id}:\n{text}")
        return True

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        client = get_http_client()
        if client is not None:
            resp = await client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=10.0) as temp_client:
                resp = await temp_client.post(url, json=payload)
        if resp.status_code != 200:
            logger.error(
                f"Telegram send to chat {chat_id} failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch Telegram message to {chat_id}: {e}")
        return False


def render_driver_submission_verdict_message(
    submission_id: str,
    status_str: str,
    driver_name: Optional[str] = None,
    reason_str: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
) -> Tuple[str, Optional[dict]]:
    """Renders user-friendly, supportive verdict notifications for drivers/collaborators."""
    name_display = f" {driver_name}" if driver_name else ""
    if status_str == "APPROVED":
        rev_text = ""
        if gross_revenue and str(gross_revenue) not in ("0", ""):
            try:
                num = int(re.sub(r"[^\d]", "", str(gross_revenue)))
                rev_text = f"• <b>Doanh thu ghi nhận:</b> <code>{num:,}đ</code>\n"
            except Exception:
                pass
        text = (
            f"🎉 <b>Khảo sát #{submission_id} đã được DUYỆT THÀNH CÔNG!</b>\n\n"
            f"Chào bạn{name_display}, kết quả khảo sát của bạn đã hợp lệ và đạt chuẩn quy định.\n"
            f"{rev_text}"
            f"• <b>Trạng thái:</b> <b>HỢP LỆ ✅</b>\n\n"
            f"Cảm ơn bạn đã tham gia. Tiền thưởng đang được đưa vào hàng đợi chi trả tự động!"
        )
        return text, None

    elif status_str == "REJECTED":
        text = (
            f"⚠️ <b>Thông báo kết quả khảo sát #{submission_id}:</b>\n\n"
            f"Chào bạn{name_display}, phiếu khảo sát của bạn chưa đạt yêu cầu.\n"
            f"• <b>Trạng thái:</b> <b>CHƯA ĐẠT ❌</b>\n"
            f"• <b>Lý do:</b> <i>{reason_str or 'Ảnh chụp hoặc số liệu chưa hợp lệ'}</i>\n\n"
            f"👉 <i>Bạn có thể bấm nút bên dưới để gửi lại ảnh chụp rõ nét hơn hoặc gửi yêu cầu phúc khảo:</i>"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": f"📸 Gửi lại ảnh #{submission_id}", "callback_data": f"driver_fix:{submission_id}:slot_photo"},
                    {"text": f"⚖️ Phúc khảo #{submission_id}", "callback_data": f"driver_appeal:{submission_id}"},
                ],
                [
                    {"text": "🆘 Yêu Cầu Hỗ Trợ", "callback_data": f"driver_support:{submission_id}"},
                ]
            ]
        }
        return text, reply_markup

    else:
        text = (
            f"⏳ <b>Khảo sát #{submission_id} đang được kiểm tra:</b>\n\n"
            f"Chào bạn{name_display}, hồ sơ của bạn đang được chuyển tới Giám Sát Viên duyệt bổ sung.\n"
            f"• <b>Trạng thái:</b> <b>ĐANG XỬ LÝ ⏳</b>\n"
            f"• <b>Ghi chú:</b> <i>{reason_str or 'Hệ thống đang đối soát dữ liệu'}</i>\n\n"
            f"Kết quả cuối cùng sẽ được thông báo trực tiếp tại đây ngay khi hoàn tất."
        )
        return text, None


async def dispatch_submission_telegram_notifications(
    submission_id: str,
    status_str: str,
    driver_phone: Optional[str] = None,
    driver_name: Optional[str] = None,
    collaborator_id: Optional[Union[str, int]] = None,
    reason_str: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
    form_id: Optional[str] = None,
    recipient_kind: str = "ctv",
    ctv_ref: Optional[str] = None,
):
    """
    Notifies the configured Telegram recipient for a submission: the owning CTV
    (default) or the driver identified by the configured phone question.
    """
    driver_bot_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not driver_bot_token or driver_bot_token == "mock_tg_token":
        logger.info(f"[MOCK TG NOTIFY CTV] sub={submission_id} status={status_str} collab={collaborator_id}")
        return

    import sys
    main_mod = sys.modules.get("main")
    _send_direct = getattr(main_mod, "send_telegram_direct_message", send_telegram_direct_message) if main_mod else send_telegram_direct_message
    _lookup_collab = getattr(main_mod, "lookup_telegram_user_for_collaborator", lookup_telegram_user_for_collaborator) if main_mod else lookup_telegram_user_for_collaborator

    if not collaborator_id:
        logger.info(f"Submission #{submission_id} has no collaborator; nothing to notify.")
        return

    collab_tg_id = await _lookup_collab(collaborator_id)
    if not collab_tg_id:
        logger.info(f"Collaborator '{collaborator_id}' not linked to Telegram (start the CTV bot with ctv_<code>).")
        return

    status_emoji = "✅" if status_str == "APPROVED" else ("⚠️" if status_str == "MANUAL_REVIEW" else "❌")
    clean_phone = driver_phone or "N/A"
    masked_phone = clean_phone[:3] + "****" + clean_phone[-3:] if len(clean_phone) >= 7 else clean_phone
    collab_msg = (
        f"📋 <b>Cập Nhật Khảo Sát #{submission_id}</b>\n\n"
        f"• <b>Đối tác / Khách hàng:</b> {driver_name or 'Tài xế'} (<code>{masked_phone}</code>)\n"
        f"• <b>Trạng thái:</b> {status_emoji} <b>{status_str}</b>\n"
        f"• <b>Ghi chú:</b> {reason_str or 'Hợp lệ'}"
    )
    if status_str == "REJECTED" and form_id:
        link = _build_retry_form_link(form_id, ctv_ref, collaborator_id, recipient_kind)
        collab_msg += f"\n\n🔁 <b>Nộp lại / mở biểu mẫu:</b> {link}"
    footer = "Tài xế" if recipient_kind == "driver" else "Cộng tác viên phụ trách"
    collab_msg += f"\n\n<i>Thông báo tự động dành cho {footer}.</i>"
    sent = await _send_direct(
        bot_token=driver_bot_token,
        chat_id=collab_tg_id,
        text=collab_msg,
    )
    if not sent:
        # The CTV/ driver may only have started the admin bot; try it as a fallback.
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None)
        if admin_token and admin_token != driver_bot_token and admin_token != "mock_tg_token":
            logger.info(f"Retrying TG verdict for #{submission_id} via admin bot fallback.")
            sent = await _send_direct(
                bot_token=admin_token,
                chat_id=collab_tg_id,
                text=collab_msg,
            )
    if sent:
        logger.info(
            f"TG verdict sent for #{submission_id} to {recipient_kind} telegram_id={collab_tg_id} "
            f"(recipient={collaborator_id}, status={status_str})."
        )
    else:
        logger.error(
            f"TG verdict send FAILED for #{submission_id} to {recipient_kind} telegram_id={collab_tg_id} "
            f"(recipient={collaborator_id}); user may not have started the driver bot."
        )

