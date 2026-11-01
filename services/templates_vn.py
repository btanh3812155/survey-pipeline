from typing import Optional, List

TEMPLATES_VN = {
    "APPROVED": (
        "✅ <b>PHIẾU KHẢO SÁT ĐÃ ĐƯỢC DUYỆT THÀNH CÔNG</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Trạng thái: <b>Hợp lệ (Approved)</b>\n"
        "• Thù lao dự kiến: <b>25.000 VNĐ</b>\n\n"
        "🎉 <i>Hệ thống đã tự động ghi nhận vào bảng thanh toán của bạn. Cảm ơn bạn!</i>"
    ),
    "MISSING_FIELDS": (
        "❌ <b>PHIẾU KHẢO SÁT CHƯA ĐẠT YÊU CẦU</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Lỗi phát hiện: <b>Thiếu thông tin bắt buộc</b>\n"
        "• Chi tiết cần bổ sung:\n{missing_list}\n\n"
        "👉 <i>Vui lòng hoàn thiện các mục trên và chụp gửi lại phiếu nhé!</i>"
    ),
    "BLURRY": (
        "❌ <b>ẢNH CHỤP KHÔNG ĐẠT (BỊ MỜ / CHÓI SÁNG)</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Lỗi phát hiện: <b>Ảnh bị rung tay hoặc lóa đèn</b>\n\n"
        "👉 <i>Chữ và số trên phiếu không đủ rõ nét để nhận diện. Bạn giữ chắc tay, chụp ở nơi đủ sáng và gửi lại nha!</i>"
    ),
    "SCREEN_PHOTO": (
        "❌ <b>PHÁT HIỆN ẢNH CHỤP MÀN HÌNH (SPOOF)</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Lỗi vi phạm: <b>Ảnh chụp từ màn hình thiết bị khác</b>\n\n"
        "👉 <i>Quy định khảo sát yêu cầu chụp trực tiếp từ phiếu giấy thực tế. Ảnh chụp màn hình sẽ không được duyệt.</i>"
    ),
    "DIGITAL_TAMPERING": (
        "❌ <b>PHÁT HIỆN DẤU HIỆU CHỈNH SỬA ẢNH (PHOTOSHOP)</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Lỗi vi phạm: <b>Ảnh có dấu hiệu can thiệp kỹ thuật số / tẩy xóa</b>\n"
        "• Chi tiết: {collaborator_note}\n\n"
        "👉 <i>Quy định bắt buộc nộp ảnh nguyên bản chụp trực tiếp từ camera. Ảnh chỉnh sửa qua phần mềm sẽ bị từ chối duyệt.</i>"
    ),
    "DUPLICATE": (
        "❌ <b>PHÁT HIỆN ẢNH TRÙNG LẶP (DUPLICATE)</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Lỗi vi phạm: <b>Phiếu này đã được nộp trước đó trên hệ thống</b>\n\n"
        "👉 <i>Vui lòng không sử dụng lại ảnh khảo sát cũ để tránh vi phạm chính sách cộng tác viên.</i>"
    ),
    "ARITHMETIC_MISMATCH": (
        "❌ <b>LỖI SAI LỆCH SỐ TIỀN TRÊN HÓA ĐƠN</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Lỗi phát hiện: <b>Tổng tiền không khớp các khoản chi tiết</b>\n"
        "• Ghi chú: {collaborator_note}\n\n"
        "👉 <i>Vui lòng kiểm tra lại số liệu trên hóa đơn và gửi lại.</i>"
    ),
    "MANUAL_REVIEW": (
        "⏳ <b>HỆ THỐNG ĐANG CHUYỂN DUYỆT THỦ CÔNG</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Trạng thái: <b>Đang chờ quản lý kiểm tra</b>\n\n"
        "👉 <i>Phiếu của bạn cần xem xét thêm. Kết quả sẽ được cập nhật trong ít phút!</i>"
    ),
    "SUSPICIOUS_NEEDS_REVIEW": (
        "⏳ <b>PHIẾU KHẢO SÁT ĐANG CHỜ QUẢN TRỊ VIÊN XÉT DUYỆT</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Trạng thái: <b>Nghi vấn cần kiểm tra lại (Suspicious)</b>\n"
        "• Chi tiết: <i>{collaborator_note}</i>\n\n"
        "👉 <i>Hệ thống đã tự động chuyển phiếu của anh tới ban quản trị để xác minh thêm. Kết quả sẽ được cập nhật trong 10-15 phút nhé!</i>"
    ),
    "APPEAL_ACCEPTED": (
        "✅ <b>ĐÃ TIẾP NHẬN YÊU CẦU KHIẾU NẠI</b>\n\n"
        "• Mã phiếu: <code>#{submission_id}</code>\n"
        "• Trạng thái: <b>Đã chuyển người thật kiểm tra lại</b>\n\n"
        "👉 <i>Quản trị viên sẽ trực tiếp đối soát ảnh gốc của anh. Kết quả sẽ được thông báo ngay tại đây!</i>"
    ),
    "APPEAL_RATE_LIMITED": (
        "⚠️ <b>ANH ĐÃ DÙNG HẾT LƯỢT KHIẾU NẠI HÔM NAY</b>\n\n"
        "• Giới hạn: <b>Tối đa 1 lần yêu cầu người thật kiểm tra / 24 giờ</b>\n\n"
        "👉 <i>Để đảm bảo tốc độ hệ thống, vui lòng kiểm tra lại ảnh chụp rõ nét và nộp phiếu mới nhé!</i>"
    ),
}


def render_telegram_verdict(
    submission_id: str,
    reason_code: str,
    missing_fields: Optional[List[str]] = None,
    collaborator_note: Optional[str] = None,
) -> str:
    template = TEMPLATES_VN.get(reason_code, TEMPLATES_VN["MISSING_FIELDS"])

    formatted_missing = ""
    if missing_fields:
        formatted_missing = "\n".join([f"  • {item}" for item in missing_fields])
    else:
        formatted_missing = "  • Không xác định"

    note = collaborator_note or "Cần đối soát lại"
    return template.format(
        submission_id=submission_id,
        missing_list=formatted_missing,
        collaborator_note=note,
    )


def render_appeal_keyboard(submission_id: str) -> dict:
    """Returns Telegram inline keyboard allowing rate-limited appeal request."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🙋 Yêu cầu người thật kiểm tra lại",
                    "callback_data": f"appeal:{submission_id}",
                }
            ]
        ]
    }
