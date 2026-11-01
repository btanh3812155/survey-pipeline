import re
from typing import List, Optional
from pydantic import BaseModel
from services.vision_ai import ExtractedReceiptFacts


class EvaluationResult(BaseModel):
    passed: bool
    status_tier: str  # 'APPROVED', 'SUSPICIOUS_NEEDS_REVIEW', 'REJECTED'
    reason_code: str
    missing_fields: List[str] = []
    collaborator_note: Optional[str] = None
    can_appeal: bool = False


def evaluate_survey_compliance(facts: ExtractedReceiptFacts) -> EvaluationResult:
    """
    Deterministically evaluates compliance rules in Python.
    Strictly isolated from LLM prompt injection attacks.
    """
    missing: List[str] = []
    # 1. Hard Rejection: Anti-Spoofing screen recapture check (Clear-cut fraud)
    if facts.is_screen_photo:
        return EvaluationResult(
            passed=False,
            status_tier="REJECTED",
            reason_code="SCREEN_PHOTO",
            missing_fields=["ảnh chụp thực tế"],
            collaborator_note="Hệ thống phát hiện ảnh chụp từ màn hình thiết bị khác. Quy định bắt buộc chụp trực tiếp từ phiếu giấy thực tế.",
            can_appeal=True,
        )

    # 2. Hard Rejection: Forensic digital manipulation / Photoshop
    if facts.is_manipulated_or_photoshopped:
        evidence = facts.tamper_evidence or "chắp vá hoặc tẩy xóa kỹ thuật số"
        return EvaluationResult(
            passed=False,
            status_tier="REJECTED",
            reason_code="DIGITAL_TAMPERING",
            missing_fields=["ảnh gốc chưa qua chỉnh sửa"],
            collaborator_note=f"Hệ thống phát hiện dấu hiệu chỉnh sửa ảnh (Photoshop/splicing): {evidence}.",
            can_appeal=True,
        )

    # 3. Reject on suspicious income/fare anomalies (Liberal rejection: retake/recheck instead of clogging admins)
    if facts.notes and "SUSPICIOUS" in facts.notes.upper():
        return EvaluationResult(
            passed=False,
            status_tier="REJECTED",
            reason_code="SUSPICIOUS_INCOME_ANOMALY",
            missing_fields=["xác thực lại số liệu"],
            collaborator_note=f"Số liệu thu nhập hoặc cước xe bất thường ({facts.notes}). Vui lòng kiểm tra lại số liệu hoặc bấm khiếu nại nếu số liệu chính xác.",
            can_appeal=True,
        )

    # 4. Hard Rejection: Missing signature
    if not facts.has_signature:
        missing.append("chữ ký khách hàng")
    # 5. Completeness: all checkboxes must be checked
    if facts.unchecked_boxes_count > 0:
        missing.append(f"{facts.unchecked_boxes_count} ô khảo sát chưa điền")

    # 6. Identification numbers
    if not facts.receipt_serial:
        missing.append("mã số phiếu / hóa đơn")

    if missing:
        return EvaluationResult(
            passed=False,
            status_tier="REJECTED",
            reason_code="MISSING_FIELDS",
            missing_fields=missing,
            collaborator_note="Phiếu khảo sát còn thiếu các thông tin bắt buộc. Vui lòng chụp bổ sung.",
            can_appeal=False,  # easily retaken, no need to waste an appeal
        )

    # 7. Reject on arithmetic discrepancy (Driver can easily re-verify and re-submit)
    if (facts.raw_total or 0) > 0 and facts.line_items:
        items_sum = sum((item.amount or 0) for item in facts.line_items)
        expected_total = items_sum + (facts.vat_amount or 0)
        if abs(expected_total - facts.raw_total) > 100:
            return EvaluationResult(
                passed=False,
                status_tier="REJECTED",
                reason_code="ARITHMETIC_MISMATCH",
                missing_fields=["tổng tiền không khớp chi tiết"],
                collaborator_note=f"Tổng tiền hóa đơn ({facts.raw_total:,}đ) không khớp tổng chi tiết ({expected_total:,}đ). Vui lòng kiểm tra lại hoặc chụp hóa đơn rõ hơn.",
                can_appeal=True,
            )

    # 8. Reject on invalid Tax ID format
    if facts.tax_id:
        clean_tax = re.sub(r"[^0-9]", "", facts.tax_id)
        if len(clean_tax) not in (10, 13, 14):
            return EvaluationResult(
                passed=False,
                status_tier="REJECTED",
                reason_code="INVALID_TAX_ID",
                missing_fields=["mã số thuế không đúng định dạng VN"],
                collaborator_note="Mã số thuế trên hóa đơn không đúng quy chuẩn (phải là 10 hoặc 14 chữ số). Vui lòng chụp lại rõ phần mã số thuế.",
                can_appeal=True,
            )
    return EvaluationResult(
        passed=True,
        status_tier="APPROVED",
        reason_code="APPROVED",
        missing_fields=[],
        collaborator_note="Phiếu khảo sát hợp lệ và đầy đủ thông tin.",
        can_appeal=False,
    )
