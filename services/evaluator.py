import re
from typing import List, Optional
from pydantic import BaseModel
from services.vision_ai import ExtractedReceiptFacts


class EvaluationResult(BaseModel):
    passed: bool
    reason_code: str
    missing_fields: List[str] = []
    collaborator_note: Optional[str] = None


def evaluate_survey_compliance(facts: ExtractedReceiptFacts) -> EvaluationResult:
    """
    Deterministically evaluates compliance rules in Python.
    Strictly isolated from LLM prompt injection attacks.
    """
    missing: List[str] = []

    # 1. Anti-Spoofing screen recapture check
    if facts.is_screen_photo:
        return EvaluationResult(
            passed=False,
            reason_code="SCREEN_PHOTO",
            missing_fields=["ảnh chụp thực tế"],
            collaborator_note="Hệ thống phát hiện ảnh chụp từ màn hình thiết bị khác. Quy định bắt buộc chụp trực tiếp từ phiếu giấy thực tế.",
        )

    # 2. Signature requirement check
    if not facts.has_signature:
        missing.append("chữ ký khách hàng")

    # 3. Completeness: all checkboxes must be checked
    if facts.unchecked_boxes_count > 0:
        missing.append(f"{facts.unchecked_boxes_count} ô khảo sát chưa điền")

    # 4. Identification numbers
    if not facts.receipt_serial:
        missing.append("mã số phiếu / hóa đơn")

    if missing:
        return EvaluationResult(
            passed=False,
            reason_code="MISSING_FIELDS",
            missing_fields=missing,
            collaborator_note="Phiếu khảo sát còn thiếu các thông tin bắt buộc.",
        )

    # 5. Arithmetic reconciliation (Sum of items + VAT == Total)
    if facts.raw_total > 0 and facts.line_items:
        items_sum = sum(item.amount for item in facts.line_items)
        expected_total = items_sum + facts.vat_amount
        # allow small 100 VND rounding threshold
        if abs(expected_total - facts.raw_total) > 100:
            return EvaluationResult(
                passed=False,
                reason_code="ARITHMETIC_MISMATCH",
                missing_fields=["tổng tiền không khớp chi tiết"],
                collaborator_note=f"Tổng tiền hóa đơn ({facts.raw_total:,}đ) không khớp tổng cộng chi tiết ({expected_total:,}đ).",
            )

    # 6. Vietnamese Tax ID format validation (if provided: 10 or 14 digits)
    if facts.tax_id:
        clean_tax = re.sub(r"[^0-9]", "", facts.tax_id)
        if len(clean_tax) not in (10, 13, 14):
            return EvaluationResult(
                passed=False,
                reason_code="INVALID_TAX_ID",
                missing_fields=["mã số thuế không đúng định dạng"],
                collaborator_note="Mã số thuế trên hóa đơn không hợp lệ theo quy chuẩn VN.",
            )

    return EvaluationResult(
        passed=True,
        reason_code="APPROVED",
        missing_fields=[],
        collaborator_note="Phiếu khảo sát hợp lệ và đầy đủ thông tin.",
    )
