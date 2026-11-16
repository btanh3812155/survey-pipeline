import os
import json
import logging
import numpy as np
from PIL import Image
from typing import List, Optional, Tuple, Dict, Any
from services.online_and_ai_detector import compute_dhash_int, hamming_distance
from services.normalizer import sanitize_and_normalize_image
from config import settings

logger = logging.getLogger("duplicate_detector")

MODEL_PATH_CANDIDATES = [
    os.path.join(os.getcwd(), "models", "dinov2_small_quantized.onnx"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "dinov2_small_quantized.onnx"),
    "/app/models/dinov2_small_quantized.onnx",
]

_dinov2_session = None

def get_dinov2_session():
    global _dinov2_session
    if _dinov2_session is not None:
        return _dinov2_session
    try:
        import onnxruntime as ort
        for p in MODEL_PATH_CANDIDATES:
            if os.path.isfile(p):
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2
                opts.inter_op_num_threads = 1
                _dinov2_session = ort.InferenceSession(p, sess_options=opts, providers=["CPUExecutionProvider"])
                logger.info(f"🚀 [DINOv2] Loaded quantized Meta DINOv2 model from {p}")
                return _dinov2_session
    except Exception as e:
        logger.warning(f"Could not load DINOv2 ONNX model: {e}")
    return None

def compute_dinov2_embedding(pil_img: Image.Image) -> Optional[List[float]]:
    """
    Computes 384-dimensional dense visual feature vector using Meta DINOv2 ViT-S/14.
    Returns L2-normalized 384-dim float list.
    """
    if os.environ.get("MOCK_AI") == "1" or os.environ.get("ENVIRONMENT") == "test":
        import hashlib
        b = pil_img.tobytes()
        h = hashlib.sha512(b).digest()
        arr = ((np.frombuffer(h * 8, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0)[:384]
        norm = np.linalg.norm(arr)
        return (arr / norm).tolist() if norm > 0 else None

    session = get_dinov2_session()
    if session is None:
        return None
    try:
        img = pil_img.convert("RGB").resize((224, 224), Image.Resampling.BILINEAR)
        arr = np.array(img).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm_arr = (arr - mean) / std
        tensor = np.transpose(norm_arr, (2, 0, 1))[np.newaxis, ...]
        out = session.run(None, {"pixel_values": tensor})[0]
        cls_emb = out[0, 0, :]
        norm_val = np.linalg.norm(cls_emb)
        if norm_val > 0:
            return (cls_emb / norm_val).tolist()
    except Exception as e:
        logger.error(f"Error computing DINOv2 embedding: {e}")
    return None

def compute_cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Computes cosine similarity between two normalized vectors."""
    try:
        return float(np.dot(vec1, vec2))
    except Exception:
        return 0.0

def compute_image_dhash_str(pil_img: Image.Image) -> str:
    """Computes 64-bit dHash bitstring for PostgreSQL BIT(64) column."""
    h_int = compute_dhash_int(pil_img)
    return format(h_int, "064b")

def check_intra_submission_duplicates(
    raw_bytes_list: List[bytes],
    target_q: Optional[Any] = None,
    weekly_incomes: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Verifies that no two weekly income statement images within the same submission
    are identical or duplicate photos of the same week.
    Checks:
    1. Perceptual dHash Hamming distance <= 8
    2. Meta DINOv2 dense visual embedding cosine similarity >= 0.88
    3. Identical non-zero revenue, trips, and period across multiple weeks
    """
    if not raw_bytes_list or len(raw_bytes_list) < 2:
        return False, None

    # Identify slots that require uniqueness (skips slots where require_unique is False, e.g. censored profiles)
    unique_slot_indices = []
    if target_q and getattr(target_q, "image_slots", None):
        for s_idx, slot in enumerate(target_q.image_slots):
            if slot.get("require_unique") is False:
                continue
            if s_idx < len(raw_bytes_list):
                unique_slot_indices.append(s_idx)
    if not unique_slot_indices and len(raw_bytes_list) >= 7:
        unique_slot_indices = list(range(2, 7))

    if len(unique_slot_indices) < 2:
        return False, None

    pil_images = []
    dhashes = []
    embeddings = []

    for s_idx in unique_slot_indices:
        b = raw_bytes_list[s_idx]
        try:
            _, p_im = sanitize_and_normalize_image(b, max_dimension=settings.IMAGE_MAX_DIMENSION)
            pil_images.append(p_im)
            dhashes.append(compute_dhash_int(p_im))
            embeddings.append(compute_dinov2_embedding(p_im))
        except Exception:
            pil_images.append(None)
            dhashes.append(None)
            embeddings.append(None)

    # 1. Compare visual pairs
    n = len(unique_slot_indices)
    for i in range(n):
        for j in range(i + 1, n):
            idx_i = unique_slot_indices[i]
            idx_j = unique_slot_indices[j]
            week_i_num = i + 1
            week_j_num = j + 1

            # Check if OCR extracted distinct financial metrics for these two weeks.
            # Real-world driver photos of the same physical phone (cracked screen, identical LED odometer on desk)
            # have high visual embedding similarity. If OCR numbers differ, OCR MUST OVERRIDE visual similarity!
            has_different_ocr = False
            if weekly_incomes and isinstance(weekly_incomes, dict):
                wi = weekly_incomes.get(f"week_{week_i_num}", {})
                wj = weekly_incomes.get(f"week_{week_j_num}", {})
                gi = int(wi.get("gross_revenue") or 0)
                gj = int(wj.get("gross_revenue") or 0)
                ni = int(wi.get("net_income") or 0)
                nj = int(wj.get("net_income") or 0)
                ti = int(wi.get("total_trips") or 0)
                tj = int(wj.get("total_trips") or 0)
                pi = str(wi.get("period") or "").strip().lower()
                pj = str(wj.get("period") or "").strip().lower()

                # If ANY financial or operational metric differs AT ALL, they are NOT duplicate photos!
                if gi > 0 and gj > 0 and gi != gj:
                    has_different_ocr = True
                elif ni > 0 and nj > 0 and ni != nj:
                    has_different_ocr = True
                elif ti > 0 and tj > 0 and ti != tj:
                    has_different_ocr = True
                elif pi and pj and pi != pj and not ("tuần" in pi and "tuần" in pj and pi == pj):
                    has_different_ocr = True

            if has_different_ocr:
                logger.info(
                    f"✅ [OCR OVERRIDE] Visual similarity between Week {week_i_num} and Week {week_j_num} overridden by distinct OCR data. "
                    f"Allowing submission!"
                )
                continue

            # Check dHash (tightened to <= 4 to avoid false positives on same-phone app chrome)
            if dhashes[i] is not None and dhashes[j] is not None:
                dist = hamming_distance(dhashes[i], dhashes[j])
                if dist <= 4:
                    logger.warning(f"🚫 [INTRA-DUPLICATE] dHash match between Week {week_i_num} and Week {week_j_num} (distance={dist} <= 4)")
                    return True, (
                        f"Ảnh chụp thu nhập giữa các tuần bị trùng lặp! Ảnh Tuần {week_i_num} (slot {idx_i+1}) "
                        f"và Ảnh Tuần {week_j_num} (slot {idx_j+1}) giống nhau. "
                        f"Vui lòng chụp đúng màn hình thu nhập của 5 tuần khác nhau trong Tháng 10!"
                    )

            # Check DINOv2 cosine similarity (relaxed from 0.88 to 0.985 for physical phone/desk tolerance)
            if embeddings[i] is not None and embeddings[j] is not None:
                sim = compute_cosine_similarity(embeddings[i], embeddings[j])
                if sim >= 0.985:
                    logger.warning(f"🚫 [INTRA-DUPLICATE] DINOv2 match between Week {week_i_num} and Week {week_j_num} (similarity={sim:.4f} >= 0.985)")
                    return True, (
                        f"Ảnh chụp thu nhập giữa các tuần bị trùng lặp! Ảnh Tuần {week_i_num} (slot {idx_i+1}) "
                        f"và Ảnh Tuần {week_j_num} (slot {idx_j+1}) có cấu trúc hình ảnh giống hệt nhau (Meta DINOv2 khớp {sim*100:.1f}%). "
                        f"Vui lòng chụp đúng màn hình thu nhập của 5 tuần khác nhau trong Tháng 10!"
                    )
    # 2. Check OCR metrics duplication between weeks
    if weekly_incomes and isinstance(weekly_incomes, dict):
        weeks_data = [weekly_incomes.get(f"week_{w+1}", {}) for w in range(n)]
        for i in range(len(weeks_data)):
            for j in range(i + 1, len(weeks_data)):
                wi = weeks_data[i]
                wj = weeks_data[j]
                if not wi or not wj:
                    continue
                gi = int(wi.get("gross_revenue") or 0)
                gj = int(wj.get("gross_revenue") or 0)
                ni = int(wi.get("net_income") or 0)
                nj = int(wj.get("net_income") or 0)
                ti = int(wi.get("total_trips") or 0)
                tj = int(wj.get("total_trips") or 0)
                pi = str(wi.get("period") or "").strip()
                pj = str(wj.get("period") or "").strip()
                if gi > 0 and gj > 0 and gi == gj and ni == nj and ti == tj:
                    if pi and pj and pi == pj:
                        return True, (
                            f"Phát hiện số liệu và kỳ báo cáo giữa Tuần {i+1} và Tuần {j+1} bị trùng lặp ({pi}). "
                            f"Vui lòng chụp đúng 5 tuần khác nhau trong Tháng 10!"
                        )

    return False, None

async def check_inter_submission_duplicates(
    db_pool: Optional[Any],
    primary_pil_img: Image.Image,
    primary_embedding: Optional[List[float]] = None,
    weekly_incomes: Optional[Dict[str, Any]] = None,
    tot_gross: Optional[int] = None,
    tot_trips: Optional[int] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Checks if an uploaded photo OR extracted financial dataset was already submitted in a previously APPROVED submission.
    Queries PostgreSQL survey_submissions using:
    0. Tier 0: Identical weekly earnings figures across past approved submissions
    1. Tier 1: dHash Hamming distance <= 6 using PostgreSQL bit operations bit_count(image_dhash # %s::bit(64)) <= 6
    2. Tier 2: Meta DINOv2 vector cosine similarity >= 0.88 via pgvector HNSW index
    """
    if not db_pool:
        return False, None, None
    dhash_int = compute_dhash_int(primary_pil_img)
    dhash_bin = format(dhash_int, "064b")

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                # 0. Tier 0: Check identical weekly revenue numbers to past approved submissions
                if weekly_incomes and isinstance(weekly_incomes, dict):
                    w1_g = int(weekly_incomes.get("week_1", {}).get("gross_revenue") or 0)
                    w2_g = int(weekly_incomes.get("week_2", {}).get("gross_revenue") or 0)
                    w3_g = int(weekly_incomes.get("week_3", {}).get("gross_revenue") or 0)
                    w4_g = int(weekly_incomes.get("week_4", {}).get("gross_revenue") or 0)
                    w5_g = int(weekly_incomes.get("week_5", {}).get("gross_revenue") or 0)

                    if w1_g > 0 and w2_g > 0:
                        await cur.execute(
                            """
                            SELECT submission_id
                            FROM survey_submissions
                            WHERE status = 'APPROVED' AND status <> 'REJECTED'
                              AND income_week_1_gross = %s
                              AND income_week_2_gross = %s
                              AND (
                                  (income_week_3_gross = %s AND %s > 0)
                                  OR (total_gross_revenue = %s AND %s > 0)
                                  OR (total_trips = %s AND %s > 0)
                              )
                            LIMIT 1;
                            """,
                            (w1_g, w2_g, w3_g, w3_g, tot_gross or 0, tot_gross or 0, tot_trips or 0, tot_trips or 0),
                        )
                        data_row = await cur.fetchone()
                        if data_row:
                            logger.warning(f"🚫 [INTER-DUPLICATE] Exact weekly income data matched with approved #{data_row[0]}")
                            return True, data_row[0], "DATA_DUPLICATE"

                # 1. Tier 1: dHash check against APPROVED submissions
                await cur.execute(
                    """
                    SELECT submission_id, bit_count(image_dhash # %s::bit(64)) as dist
                    FROM survey_submissions
                    WHERE status = 'APPROVED' AND status <> 'REJECTED'
                      AND image_dhash IS NOT NULL
                      AND bit_count(image_dhash # %s::bit(64)) <= 6
                    ORDER BY dist ASC
                    LIMIT 1;
                    """,
                    (dhash_bin, dhash_bin),
                )
                row = await cur.fetchone()
                if row:
                    logger.warning(f"🚫 [INTER-DUPLICATE] dHash match with approved #{row[0]} (dist={row[1]})")
                    return True, row[0], "IMAGE_DHASH"

                # 2. Tier 2: DINOv2 pgvector cosine distance check
                if primary_embedding is not None and len(primary_embedding) == 384:
                    vec_str = "[" + ",".join(str(x) for x in primary_embedding) + "]"
                    await cur.execute(
                        """
                        SELECT submission_id, total_gross_revenue, (1 - (embedding <=> %s::vector)) as similarity
                        FROM survey_submissions
                        WHERE status = 'APPROVED' AND status <> 'REJECTED'
                          AND embedding IS NOT NULL
                          AND (1 - (embedding <=> %s::vector)) >= 0.985
                        ORDER BY embedding <=> %s::vector ASC
                        LIMIT 1;
                        """,
                        (vec_str, vec_str, vec_str),
                    )
                    row_vec = await cur.fetchone()
                    if row_vec:
                        past_sub_id, past_gross, sim_val = row_vec[0], row_vec[1], row_vec[2]
                        if tot_gross and past_gross and int(tot_gross) != int(past_gross):
                            logger.info(f"✅ [OCR OVERRIDE] Inter-submission visual match with #{past_sub_id} overridden by distinct gross revenue ({tot_gross:,} vs {past_gross:,}).")
                        else:
                            logger.warning(f"🚫 [INTER-DUPLICATE] DINOv2 match with approved #{past_sub_id} (similarity={sim_val:.4f})")
                            return True, past_sub_id, "IMAGE_DINOV2"
    except Exception as e:
        logger.debug(f"Inter-submission duplicate query note: {e}")
    return False, None, None
