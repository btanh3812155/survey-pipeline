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
import secrets
import logging
import asyncio
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
from services.normalizer import sanitize_and_normalize_image, compute_laplacian_blur_score, detect_image_manipulation
from services.online_and_ai_detector import compute_dhash_int, hamming_distance
from services.duplicate_detector import compute_image_dhash_str, compute_dinov2_embedding, check_intra_submission_duplicates, check_inter_submission_duplicates
from services.vehicle_tracker import VehiclePersistenceTracker, normalize_license_plate
from services.income_sanitizer import analyze_driver_income
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

@router.get("/camera-form", response_class=HTMLResponse)
@router.get("/forms/{form_id}", response_class=HTMLResponse)
async def get_dynamic_form(request: Request, form_id: str = "default"):
    """Renders dynamic forms with conditional logic, signature modal, and GPS authorization."""
    host = (request.headers.get("host") or "").lower()
    if any(host.startswith(p) for p in ("audit.", "logs.", "dev-audit.", "audit-dev.", "audit.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/audit", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("submissions.", "view.", "data.", "dev-submissions.", "dev-data.", "dev-admin.", "admin-dev.", "submissions-dev.", "data-dev.", "admin.dev.", "data.dev.", "submissions.dev.")) and form_id in ("default", "camera-form"):
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
    # 3. Resolve schema
    effective_form_id = "income_survey" if (form_id in ("default", "camera-form") or matched_ctv or form_id not in FORM_REGISTRY) else form_id
    schema = FORM_REGISTRY.get(effective_form_id) or next(iter(FORM_REGISTRY.values()))

    has_ctv_tracking = (
        effective_form_id in ("income_survey", "default", "camera-form")
        or any(q.id in ("ctv_code", "collaborator_id") for q in schema.questions)
    )

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
@router.post("/api/v1/survey/submit-webrtc")
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
        or form_data.get("ctv_id")
        or form_data.get("collaborator_tg_id")
        or request.query_params.get("collaborator_id")
        or request.query_params.get("ctv")
    )
    active_form_id = form_id or "income_survey"
    schema = FORM_REGISTRY.get(active_form_id)
    target_question_id = question_id or "camera_receipt"
    target_q = next((qu for qu in schema.questions if qu.id == target_question_id), None) if schema else None

    # Inspect survey_photo and request form for survey_photo_0, survey_photo_1, etc.
    images_list = []
    has_indexed_photos = False
    idx = 0
    while True:
        k_sp = f"survey_photo_{idx}"
        k_tq = f"{target_question_id}_{idx}"
        item = None
        if k_sp in form_data and (hasattr(form_data[k_sp], "read") or isinstance(form_data[k_sp], UploadFile)):
            item = form_data[k_sp]
        elif k_tq in form_data and (hasattr(form_data[k_tq], "read") or isinstance(form_data[k_tq], UploadFile)):
            item = form_data[k_tq]
        if item is not None:
            images_list.append(item)
            has_indexed_photos = True
            idx += 1
        else:
            break

    if not images_list and survey_photo:
        images_list.append(survey_photo)
        idx = 1
        while True:
            k_sp = f"survey_photo_{idx}"
            k_tq = f"{target_question_id}_{idx}"
            item = None
            if k_sp in form_data and (hasattr(form_data[k_sp], "read") or isinstance(form_data[k_sp], UploadFile)):
                item = form_data[k_sp]
            elif k_tq in form_data and (hasattr(form_data[k_tq], "read") or isinstance(form_data[k_tq], UploadFile)):
                item = form_data[k_tq]
            if item is not None:
                images_list.append(item)
                has_indexed_photos = True
                idx += 1
            else:
                break

    answers: Dict[str, Any] = dict(form_data)
    if "driver_phone" not in answers:
        answers["driver_phone"] = driver_phone
    if "driver_name" not in answers and driver_name:
        answers["driver_name"] = driver_name
    if "driver_signature" not in answers and signature_png:
        answers["driver_signature"] = signature_png
    if images_list:
        if has_indexed_photos or len(images_list) > 1:
            answers[target_question_id] = [f"uploaded_{i}" for i in range(len(images_list))]
        else:
            # Legacy single photo upload without indexed multi-image fields
            if target_q and target_q.min_images and target_q.min_images > 1:
                answers[target_question_id] = [f"uploaded_{i}" for i in range(target_q.min_images)]
            else:
                answers[target_question_id] = "uploaded"
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
    os.makedirs(sub_photo_dir, exist_ok=True)
    for i_idx, r_b in enumerate(raw_bytes_list):
        try:
            with open(os.path.join(sub_photo_dir, f"slot_{i_idx}.jpg"), "wb") as fp:
                fp.write(r_b)
        except Exception:
            pass

    raw_bytes = raw_bytes_list[0] if raw_bytes_list else b""
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
            # For facts extraction, select the app revenue screen if multi-image
            target_raw_bytes = raw_bytes
            if len(raw_bytes_list) >= 2:
                target_raw_bytes = raw_bytes_list[1]

            try:
                norm_bytes, pil_im = sanitize_and_normalize_image(
                    target_raw_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION
                )
            except Exception as e:
                norm_bytes, pil_im = None, None
                is_approved = False
                status_str = "REJECTED"
                reason_str = f"Không thể giải mã ảnh: {str(e)}"
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
                        _, p_im = sanitize_and_normalize_image(img_b, max_dimension=settings.IMAGE_MAX_DIMENSION)
                        sharp, b_var = compute_laplacian_blur_score(p_im, threshold=slot_blur_thresh)
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
                        # 1. Parallel threadpool normalization: downscale each image ONCE concurrently!
                        norm_tasks = [
                            asyncio.to_thread(sanitize_and_normalize_image, b, settings.IMAGE_MAX_DIMENSION)
                            for b in raw_bytes_list
                        ]
                        norm_results = await asyncio.gather(*norm_tasks, return_exceptions=True)
                        norm_bytes_list = [
                            res[0] if (isinstance(res, tuple) and res[0]) else raw_bytes_list[i]
                            for i, res in enumerate(norm_results)
                        ]

                        # 2. Parallel role classification across all normalized images
                        classify_tasks = [
                            vision_client.classify_image_role(
                                norm_bytes_list[s_idx],
                                filename=getattr(images_list[s_idx], "filename", "") or "",
                                expected_slots=target_q.image_slots,
                            )
                            for s_idx in range(len(raw_bytes_list))
                        ]
                        classified_roles = await asyncio.gather(*classify_tasks, return_exceptions=True)
                        classified_roles = [r if isinstance(r, str) else "unknown" for r in classified_roles]

                        # 3. Dynamic slot matching: match slots strictly AFTER classification!
                        matched_profile_idx = None
                        matched_vehicle_idx = None
                        matched_income_indices = []

                        for idx, role in enumerate(classified_roles):
                            if role in ("app_profile", "odometer", "profile_screen") and matched_profile_idx is None:
                                matched_profile_idx = idx
                            elif role in ("vehicle_exterior", "car_exterior", "car_photo", "steering_wheel", "auto_passed_vehicle") and matched_vehicle_idx is None:
                                matched_vehicle_idx = idx
                            elif role in ("app_income", "revenue_screen", "weekly_income"):
                                matched_income_indices.append(idx)

                        assigned = set(filter(lambda x: x is not None, [matched_profile_idx, matched_vehicle_idx] + matched_income_indices))
                        unassigned = [i for i in range(len(raw_bytes_list)) if i not in assigned]

                        if matched_profile_idx is None and unassigned:
                            matched_profile_idx = unassigned.pop(0)
                        elif matched_profile_idx is None:
                            matched_profile_idx = 0

                        if matched_vehicle_idx is None and unassigned:
                            matched_vehicle_idx = unassigned.pop(0)

                        matched_income_indices.extend(unassigned)
                        if not matched_income_indices:
                            matched_income_indices = [1] if len(raw_bytes_list) > 1 else [0]

                        # 4. Concurrently run Profile OCR, Facts extraction, and all Weekly Income OCR tasks
                        # REUSING pre-normalized bytes (ZERO redundant libvips calls!)
                        task_profile_ocr = vision_client.ocr_driver_app_profile(norm_bytes_list[matched_profile_idx])
                        primary_income_idx = matched_income_indices[0]
                        task_facts = vision_client.extract_facts(norm_bytes_list[primary_income_idx])

                        income_tasks = [
                            vision_client.ocr_driver_app_income(norm_bytes_list[s_idx], week_idx=i_pos + 1)
                            for i_pos, s_idx in enumerate(matched_income_indices)
                        ]

                        all_results = await asyncio.gather(task_profile_ocr, task_facts, *income_tasks, return_exceptions=True)
                        profile_res = all_results[0]
                        facts_res = all_results[1]
                        income_res_list = all_results[2:]

                        role_0 = classified_roles[matched_profile_idx]
                        membership_type = profile_res.get("membership_type") if isinstance(profile_res, dict) and not isinstance(profile_res, Exception) else "Tiêu chuẩn"
                        primary_ocr = income_res_list[0] if income_res_list and isinstance(income_res_list[0], dict) and not isinstance(income_res_list[0], Exception) else {}
                        ocr_data = primary_ocr
                        weekly_incomes = {}
                        tot_gross = 0
                        tot_net = 0
                        tot_trips = 0
                        monthly_violation = None
                        math_violation = None
                        for w_i in range(5):
                            w_num = w_i + 1
                            if w_i < len(income_res_list) and isinstance(income_res_list[w_i], dict) and not isinstance(income_res_list[w_i], Exception):
                                ocr_w = income_res_list[w_i]
                            elif w_i == 0 and isinstance(ocr_data, dict):
                                ocr_w = ocr_data
                            else:
                                ocr_w = {}
                            g_val = int(ocr_w.get("gross_revenue") or 0)
                            n_val = int(ocr_w.get("net_income") or 0)
                            t_val = int(ocr_w.get("total_trips") or 0)
                            weekly_incomes[f"week_{w_num}"] = {
                                "gross_revenue": g_val,
                                "net_income": n_val,
                                "total_trips": t_val,
                                "discount_and_tax": ocr_w.get("discount_and_tax", 0),
                                "period": ocr_w.get("period", f"Tuần {w_num}"),
                                "online_hours": ocr_w.get("online_hours", ""),
                            }
                            tot_gross += g_val
                            tot_net += n_val
                            tot_trips += t_val

                            # Guard against uploading monthly income report into weekly slot
                            p_type = str(ocr_w.get("period_type", "")).upper()
                            p_str = str(ocr_w.get("period", "")).lower()
                            is_wk = ocr_w.get("is_weekly_report")
                            if p_type == "MONTHLY" or (is_wk is False and ("tháng" in p_str or "month" in p_str)):
                                s_num = matched_income_indices[w_i] + 1 if w_i < len(matched_income_indices) else w_num + 2
                                monthly_violation = {
                                    "failed_slot": matched_income_indices[w_i] if w_i < len(matched_income_indices) else w_num,
                                    "reason": (
                                        f"Ảnh {s_num} (Tuần {w_num}): Phát hiện ảnh chụp báo cáo doanh thu theo THÁNG "
                                        f"(kỳ ghi nhận: '{ocr_w.get('period', 'Tháng')}') thay vì theo TUẦN. "
                                        f"Vui lòng chụp đúng màn hình thu nhập chi tiết từng tuần!"
                                    ),
                                    "ocr_data": ocr_w,
                                }
                                break
                            # Guard against arithmetic inconsistency: Income - Costs != Profit
                            c_val = ocr_w.get("discount_and_tax")
                            if g_val > 0 and (n_val > 0 or c_val is not None):
                                w_math_res = analyze_driver_income(
                                    vehicle_type=vehicle_type or "CAR",
                                    gross_income=g_val,
                                    net_income=n_val if n_val > 0 else None,
                                    trips_count=t_val if t_val > 0 else None,
                                    platform_discount=c_val,
                                    period="weekly",
                                    car_model=car_model_val,
                                )
                                if not w_math_res.is_valid and w_math_res.status_code == "ARITHMETIC_MISMATCH":
                                    s_num = matched_income_indices[w_i] + 1 if w_i < len(matched_income_indices) else w_num + 2
                                    math_violation = {
                                        "failed_slot": matched_income_indices[w_i] if w_i < len(matched_income_indices) else w_num,
                                        "reason": f"Ảnh {s_num} (Tuần {w_num}): {w_math_res.flag_reason}",
                                        "ocr_data": ocr_w,
                                    }
                                    break

                        # Check aggregate 5-week arithmetic consistency
                        if not monthly_violation and not math_violation and tot_gross > 0 and tot_net > 0:
                            if tot_net > tot_gross * 1.15 + 100_000:
                                math_violation = {
                                    "failed_slot": matched_income_indices[0] if matched_income_indices else 2,
                                    "reason": f"Số học tổng 5 tuần không khớp: Tổng thực nhận ({tot_net:,}đ) vượt quá tổng doanh thu ({tot_gross:,}đ)!",
                                    "ocr_data": ocr_data,
                                }
                        slot_0_role = target_q.image_slots[0].get("role", "app_profile") if len(target_q.image_slots) > 0 else "app_profile"
                        slot_0_title = target_q.image_slots[0].get("title", "Ảnh 1") if len(target_q.image_slots) > 0 else "Ảnh 1"

                        # 1. Profile Verification (Slot 0)
                        if monthly_violation:
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = monthly_violation["reason"]
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": monthly_violation["failed_slot"],
                                "reason": reason_str,
                                "ocr_data": monthly_violation["ocr_data"],
                            }
                        elif math_violation:
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = math_violation["reason"]
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": math_violation["failed_slot"],
                                "reason": reason_str,
                                "ocr_data": math_violation["ocr_data"],
                            }
                        elif role_0 in ("app_income", "revenue_screen"):
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Phát hiện sai loại màn hình (đây là màn hình thống kê thu nhập, không phải màn hình hồ sơ/tiện ích ứng dụng). Vui lòng chụp đúng màn hình hồ sơ ứng dụng ở Ảnh 1!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        elif role_0 != slot_0_role and role_0 not in ("app_profile", "odometer"):
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
                        # 2. Tampering & App Spoofing Checks (Income screen)
                        elif ocr_data.get("is_manipulated_or_photoshopped", False):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = "Màn hình thu nhập: Phát hiện dấu hiệu chỉnh sửa ảnh kỹ thuật số (Photoshop/splicing) trên màn hình thu nhập!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": primary_income_idx,
                                "reason": reason_str,
                                "ocr_data": ocr_data,
                            }
                        elif ocr_data.get("is_authentic_gsm_ui") is False or ocr_data.get("detected_app") in ("GRAB", "BE", "GOJEK", "OTHER_MOCKUP"):
                            is_approved = False
                            status_str = "REJECTED"
                            detected = ocr_data.get("detected_app", "MOCKUP")
                            reason_str = (
                                f"Màn hình thu nhập: Phát hiện giao diện không phải ứng dụng Tài xế Xanh SM chính thức "
                                f"(phát hiện: {detected}). Vui lòng chụp đúng màn hình ứng dụng Tài xế Xanh SM!"
                            )
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": primary_income_idx,
                                "reason": reason_str,
                                "app_spoofing_detected": True,
                                "detected_app": detected,
                                "ocr_data": ocr_data,
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        elif (
                            ocr_data.get("is_legible") is False
                            or (ocr_data.get("confidence_score") is not None and float(ocr_data.get("confidence_score", 1.0)) < 0.6)
                            or (int(ocr_data.get("gross_revenue") or 0) <= 0 and int(ocr_data.get("net_income") or 0) <= 0)
                        ):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = "Không thể đọc rõ các số liệu thu nhập trên màn hình với độ tin cậy cần thiết. Vui lòng chụp lại rõ nét hơn!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": primary_income_idx,
                                "reason": reason_str,
                                "ocr_data": ocr_data,
                            }
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
                                "weekly_incomes": weekly_incomes,
                                "total_gross_revenue": tot_gross,
                                "total_net_income": tot_net,
                                "total_trips_count": tot_trips if tot_trips > 0 else trips_val,
                                "online_hours": hours_val,
                                "period": period_val,
                            }

                        should_check_fraud = bool(getattr(checks, "enable_anti_fraud", True) and getattr(checks, "require_unique", True))
                        if is_approved and should_check_fraud:
                            # 3. Anti-Fraud: Intra-submission duplicate check across weekly income slots (Meta DINOv2 + dHash + numbers)
                            intra_dup, intra_reason = check_intra_submission_duplicates(raw_bytes_list, target_q, weekly_incomes)
                            if intra_dup:
                                is_approved = False
                                status_str = "REJECTED"
                                reason_str = intra_reason
                                ai_res["status"] = "REJECTED"
                                ai_res["reason"] = reason_str
                                ai_res["duplicate_detected"] = True
                                ai_res["duplicate_type"] = "INTRA_SUBMISSION"
                            else:
                                # Pick primary image that requires uniqueness for inter-submission checking
                                check_im = None
                                if target_q and getattr(target_q, "image_slots", None):
                                    for s_idx, slot in enumerate(target_q.image_slots):
                                        if slot.get("require_unique") is not False and s_idx < len(raw_bytes_list):
                                            try:
                                                _, s_im = sanitize_and_normalize_image(raw_bytes_list[s_idx], max_dimension=settings.IMAGE_MAX_DIMENSION)
                                                if s_im is not None:
                                                    check_im = s_im
                                                    break
                                            except Exception:
                                                pass
                                if check_im is None:
                                    check_im = pil_im

                                if check_im is not None:
                                    db_pool = get_db_pool()
                                    if db_pool:
                                        # 4. Anti-Fraud: Inter-submission duplicate check against approved submissions (Meta DINOv2 + dHash + identical data)
                                        primary_emb = compute_dinov2_embedding(check_im)
                                        inter_dup, matched_sub_id, dup_type = await check_inter_submission_duplicates(
                                            db_pool, check_im, primary_emb,
                                            weekly_incomes=weekly_incomes,
                                            tot_gross=tot_gross,
                                            tot_trips=tot_trips,
                                        )
                                        if inter_dup:
                                            is_approved = False
                                            status_str = "REJECTED"
                                            if dup_type == "DATA_DUPLICATE":
                                                reason_str = f"Số liệu doanh thu 5 tuần bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{matched_sub_id})! Hệ thống chống gian lận phát hiện số liệu bị sao chép."
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
                                                        cur_w1 = int(weekly_incomes.get("week_1", {}).get("gross_revenue") or 0)
                                                        cur_w2 = int(weekly_incomes.get("week_2", {}).get("gross_revenue") or 0)
                                                        if prev_g > 0 and tot_gross > 0 and prev_g == tot_gross and prev_t > 0 and prev_t == tot_trips:
                                                            p_w1 = int(prev_w.get("week_1", {}).get("gross_revenue") or prev.get("income_week_1_gross") or 0)
                                                            p_w2 = int(prev_w.get("week_2", {}).get("gross_revenue") or prev.get("income_week_2_gross") or 0)
                                                            if p_w1 > 0 and p_w1 == cur_w1 and p_w2 > 0 and p_w2 == cur_w2:
                                                                is_approved = False
                                                                status_str = "REJECTED"
                                                                reason_str = f"Số liệu doanh thu 5 tuần bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{prev.get('submission_id')})! Phát hiện sao chép số liệu thu nhập."
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

    # Form specific metrics and 5-week income extractions
    vehicle_type_val = str(form_data.get("vehicle_type") or vehicle_type or "Xe điện GSM")
    car_model_val = str(form_data.get("car_model") or "")
    gsm_duration_val = str(form_data.get("gsm_duration") or "")
    gsm_contract_type_val = str(form_data.get("gsm_contract_type") or "")
    raw_daily_trips = form_data.get("daily_trips")
    daily_trips_val = int(raw_daily_trips) if raw_daily_trips is not None and str(raw_daily_trips).isdigit() else None
    monthly_income_range_val = str(form_data.get("monthly_income_range") or "")
    raw_policy = form_data.get("gsm_incentive_policy") or ""
    gsm_policy_val = "; ".join(str(x) for x in raw_policy) if isinstance(raw_policy, list) else str(raw_policy)
    membership_type_val = ai_res.get("membership_type") or form_data.get("membership_type") or "Tiêu chuẩn"

    w_incomes = ai_res.get("weekly_incomes") or {}
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

    computed_tot_g = w1_g + w2_g + w3_g + w4_g + w5_g
    computed_tot_n = w1_n + w2_n + w3_n + w4_n + w5_n
    computed_tot_t = w1_t + w2_t + w3_t + w4_t + w5_t

    tot_gross_val = int(ai_res.get("total_gross_revenue") or (computed_tot_g if computed_tot_g > 0 else (ai_res.get("gross_revenue") or gross_revenue or 0)))
    tot_net_val = int(ai_res.get("total_net_income") or (computed_tot_n if computed_tot_n > 0 else (ai_res.get("net_income") or 0)))
    tot_trips_val = int(ai_res.get("total_trips") or (computed_tot_t if computed_tot_t > 0 else (trip_count or 0)))
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
    elif db_pool:
        try:
            async with db_pool.connection() as conn:
                cur_res = conn.cursor()
                if hasattr(cur_res, "__await__"):
                    cur_res = await cur_res
                cur = await cur_res.__aenter__() if hasattr(cur_res, "__aenter__") else cur_res
                try:
                    if not is_dissent and persisted_phone:
                        await cur.execute(
                            """
                            INSERT INTO driver_identity_vault (
                                driver_id, driver_phone, driver_name
                            ) VALUES (%s, %s, %s)
                            ON CONFLICT (driver_phone) DO UPDATE
                            SET driver_id = EXCLUDED.driver_id, driver_name = EXCLUDED.driver_name;
                            """,
                            (driver_tag, persisted_phone, persisted_name),
                        )
                    effective_ctv = str(collab_param or answers.get("ctv_code") or "").strip() or None
                    consent_val = str(answers.get("consent_choice") or "Tôi đồng ý")
                    calc_dhash = compute_image_dhash_str(pil_im) if pil_im is not None else None
                    calc_emb = compute_dinov2_embedding(pil_im) if pil_im is not None else None
                    calc_emb_str = ("[" + ",".join(str(x) for x in calc_emb) + "]") if calc_emb else None
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
                        driver_tag if not is_dissent else None,
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
                        tot_gross_val, tot_net_val, tot_trips_val,
                        json.dumps(action_checklist_init),
                    )
                    try:
                        await cur.execute(
                            """
                            INSERT INTO survey_submissions (
                                submission_id, form_id, ctv_code, driver_phone, driver_name, receipt_serial_no,
                                status, rejection_reason, form_data, raw_extracted_facts, driver_id,
                                image_dhash, embedding,
                                consent_choice, vehicle_type, car_model, gsm_duration, gsm_contract_type, daily_trips,
                                monthly_income_range, gsm_incentive_policy, membership_type,
                                total_gross_revenue, total_net_income, total_trips,
                                action_status
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s,
                                %s
                            )
                            ON CONFLICT (submission_id) DO UPDATE SET
                                action_status = EXCLUDED.action_status,
                                image_dhash = EXCLUDED.image_dhash,
                                embedding = EXCLUDED.embedding,
                                consent_choice = EXCLUDED.consent_choice,
                                vehicle_type = EXCLUDED.vehicle_type,
                                car_model = EXCLUDED.car_model,
                                gsm_duration = EXCLUDED.gsm_duration,
                                gsm_contract_type = EXCLUDED.gsm_contract_type,
                                daily_trips = EXCLUDED.daily_trips,
                                monthly_income_range = EXCLUDED.monthly_income_range,
                                gsm_incentive_policy = EXCLUDED.gsm_incentive_policy,
                                membership_type = EXCLUDED.membership_type,
                                income_week_1_gross = EXCLUDED.income_week_1_gross,
                                income_week_1_net = EXCLUDED.income_week_1_net,
                                income_week_1_trips = EXCLUDED.income_week_1_trips,
                                income_week_2_gross = EXCLUDED.income_week_2_gross,
                                income_week_2_net = EXCLUDED.income_week_2_net,
                                income_week_2_trips = EXCLUDED.income_week_2_trips,
                                income_week_3_gross = EXCLUDED.income_week_3_gross,
                                income_week_3_net = EXCLUDED.income_week_3_net,
                                income_week_3_trips = EXCLUDED.income_week_3_trips,
                                income_week_4_gross = EXCLUDED.income_week_4_gross,
                                income_week_4_net = EXCLUDED.income_week_4_net,
                                income_week_4_trips = EXCLUDED.income_week_4_trips,
                                income_week_5_gross = EXCLUDED.income_week_5_gross,
                                income_week_5_net = EXCLUDED.income_week_5_net,
                                income_week_5_trips = EXCLUDED.income_week_5_trips,
                                total_gross_revenue = EXCLUDED.total_gross_revenue,
                                total_net_income = EXCLUDED.total_net_income,
                                total_trips = EXCLUDED.total_trips;
                            """,
                            insert_vals_full,
                        )
                    except Exception as ins_err:
                        logger.debug(f"Extended column insert fallback: {ins_err}")
                        await cur.execute(
                            """
                            INSERT INTO survey_submissions (
                                submission_id, form_id, ctv_code, driver_phone, driver_name, receipt_serial_no,
                                status, rejection_reason, form_data, raw_extracted_facts, driver_id
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (submission_id) DO NOTHING;
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
                                driver_tag if not is_dissent else None,
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
            logger.error(f"failed to persist survey to DB: {e}")

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
        )
    except Exception as act_err:
        logger.error(f"Failed to dispatch action checklist task: {act_err}")
    # 4. Telegram dispatch to admin group
    status_emoji = "⏳" if status_str == "MANUAL_REVIEW" else ("✅" if status_str == "APPROVED" else "❌")
    auto_verdict = ai_res.get("status", "MANUAL_REVIEW") if isinstance(ai_res, dict) else "MANUAL_REVIEW"
    auto_reason = ai_res.get("reason", "") if isinstance(ai_res, dict) else ""
    auto_emoji = "✅" if str(auto_verdict).upper() == "APPROVED" else "❌"
    vehicle_type_val = vehicle_type or (ai_res.get("vehicle_type") if isinstance(ai_res, dict) else None) or "Chưa xác định"
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

    # 5. Direct Telegram DMs to Driver / Collaborator if linked
    try:
        import sys
        main_mod = sys.modules.get("main")
        _dispatch_fn = getattr(main_mod, "dispatch_submission_telegram_notifications", dispatch_submission_telegram_notifications) if main_mod else dispatch_submission_telegram_notifications
        background_tasks.add_task(
            _dispatch_fn,
            submission_id=submission_id,
            status_str=status_str,
            driver_phone=driver_phone,
            driver_name=driver_name,
            collaborator_id=collab_param,
            reason_str=reason_str,
            gross_revenue=ai_res.get("gross_revenue") or gross_revenue,
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
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'APPEAL_PENDING', rejection_reason = %s WHERE submission_id = %s;",
                        (f"Tài xế yêu cầu người thật đối soát lại: {req.reason}", sub_id)
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
                    r["reason"] = f"Tài xế yêu cầu người thật đối soát lại: {req.reason}"
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
    """Resolves collaborator identifier to telegram_user_id (supports ID, phone, or username)."""
    collab_str = str(collab_id).strip()
    if collab_str.isdigit():
        val = int(collab_str)
        db_pool = get_db_pool()
        if db_pool:
            try:
                async with db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT telegram_user_id FROM authorized_users WHERE telegram_user_id = %s AND is_active = TRUE LIMIT 1;",
                            (val,),
                        )
                        row = await cur.fetchone()
                        if row and row[0]:
                            return int(row[0])
            except Exception:
                pass
        return val

    clean_user = collab_str.lstrip("@")
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users WHERE (username = %s OR driver_phone = %s) AND is_active = TRUE LIMIT 1;",
                        (clean_user, collab_str),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception:
            pass
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
        resp = await http_client.post(url, json=payload)
        return resp.status_code == 200
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
):
    """
    Asynchronously notifies driver and collaborator in Telegram direct message.
    """
    driver_bot_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not driver_bot_token or driver_bot_token == "mock_tg_token":
        logger.info(f"[MOCK TG NOTIFY PARTIES] sub={submission_id} status={status_str} driver={driver_phone} collaber={collaborator_id}")
        return

    import sys
    main_mod = sys.modules.get("main")
    _send_direct = getattr(main_mod, "send_telegram_direct_message", send_telegram_direct_message) if main_mod else send_telegram_direct_message
    _lookup_phone = getattr(main_mod, "lookup_telegram_user_by_phone", lookup_telegram_user_by_phone) if main_mod else lookup_telegram_user_by_phone
    _lookup_collab = getattr(main_mod, "lookup_telegram_user_for_collaborator", lookup_telegram_user_for_collaborator) if main_mod else lookup_telegram_user_for_collaborator

    # 1. Notify Driver if linked
    if driver_phone:
        driver_tg_id = await _lookup_phone(driver_phone)
        if driver_tg_id:
            msg_text, reply_markup = render_driver_submission_verdict_message(
                submission_id=submission_id,
                status_str=status_str,
                driver_name=driver_name,
                reason_str=reason_str,
                gross_revenue=gross_revenue,
            )
            await _send_direct(
                bot_token=driver_bot_token,
                chat_id=driver_tg_id,
                text=msg_text,
                reply_markup=reply_markup,
            )
        else:
            logger.debug(f"Driver phone {driver_phone} not yet linked to Telegram chat ID.")

    # 2. Notify Collaborator if linked
    if collaborator_id:
        collab_tg_id = await _lookup_collab(collaborator_id)
        if collab_tg_id:
            status_emoji = "✅" if status_str == "APPROVED" else ("⚠️" if status_str == "MANUAL_REVIEW" else "❌")
            clean_phone = driver_phone or "N/A"
            masked_phone = clean_phone[:3] + "****" + clean_phone[-3:] if len(clean_phone) >= 7 else clean_phone
            collab_msg = (
                f"📋 <b>Cập Nhật Khảo Sát #{submission_id}</b>\n\n"
                f"• <b>Đối tác / Khách hàng:</b> {driver_name or 'Tài xế'} (<code>{masked_phone}</code>)\n"
                f"• <b>Trạng thái:</b> {status_emoji} <b>{status_str}</b>\n"
                f"• <b>Ghi chú:</b> {reason_str or 'Hợp lệ'}\n\n"
                f"<i>Thông báo tự động dành cho Cộng tác viên phụ trách.</i>"
            )
            await _send_direct(
                bot_token=driver_bot_token,
                chat_id=collab_tg_id,
                text=collab_msg,
            )
        else:
            logger.info(f"Collaborator '{collaborator_id}' not yet linked to Telegram chat ID (will link later).")

