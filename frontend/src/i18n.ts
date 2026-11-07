export interface TranslationStrings {
  code: string;
  gps_prompt: string;
  gps_btn: string;
  gps_getting: string;
  gps_acquiring: string;
  gps_acquired: string;
  gps_denied: string;
  gps_err: string;
  mode_label: string;
  mode_prod: string;
  mode_test: string;
  btn_to_test: string;
  btn_to_prod: string;
  bypass_label: string;
  btn_history_nav: string;
  sig_prompt: string;
  sig_done: string;
  sig_title: string;
  sig_sub: string;
  sig_hint: string;
  btn_close: string;
  btn_clear: string;
  btn_confirm: string;
  btn_submit: string;
  btn_submitting: string;
  btn_reset: string;
  reset_title: string;
  reset_desc: string;
  btn_cancel: string;
  btn_confirm_reset: string;
  reset_done: string;
  cam_off: string;
  btn_cam_on: string;
  btn_cam_off: string;
  cam_hint: string;
  btn_retake: string;
  btn_sample_img: string;
  btn_sample_ans: string;
  decree_footer: string;
  history_title: string;
  history_ph: string;
  history_query: string;
  history_prompt: string;
  history_empty: string;
  missing_required: string;
  verdict_approved: string;
  verdict_rejected: string;
  server_err: string;
  status_approved: string;
  status_manual_review: string;
  status_rejected: string;
  modal_approved_title: string;
  modal_approved_desc: string;
  modal_review_title: string;
  modal_review_desc: string;
  modal_rejected_title: string;
  modal_rejected_desc: string;
  btn_open_telegram: string;
  btn_view_history_web: string;
  btn_new_submission: string;
  locked_notice: string;
}

export type SupportedLanguage = 'vi' | 'en';

export const I18N: Record<SupportedLanguage, TranslationStrings> = {
  vi: {
    code: 'VI',
    gps_prompt: 'Cho phép truy cập vị trí để ghi nhận địa điểm khảo sát',
    gps_btn: 'Bật vị trí',
    gps_getting: 'Đang xác định vị trí...',
    gps_acquiring: 'Đang lấy vị trí...',
    gps_acquired: '📍 Đã xác nhận vị trí',
    gps_denied: 'Quyền vị trí bị từ chối. Vui lòng cho phép trong cài đặt.',
    gps_err: 'Chưa thể lấy vị trí. Vui lòng thử lại.',
    mode_label: 'Chế độ biểu mẫu:',
    mode_prod: 'Chính thức',
    mode_test: 'Thử nghiệm (Không lưu hệ thống)',
    btn_to_test: 'Bật thử nghiệm',
    btn_to_prod: 'Về Chính thức',
    bypass_label: 'Bỏ qua kiểm tra ảnh (Test nhanh dữ liệu)',
    btn_history_nav: 'Xem lịch sử nộp',
    sig_prompt: 'Bấm vào đây để ký tên trên giấy',
    sig_done: 'Chữ ký đã ghi nhận (Bấm vào đây để ký lại)',
    sig_title: 'Bảng Ký Tên Khảo Sát',
    sig_sub: 'Dùng ngón tay ký tên',
    sig_hint: 'Ký tên phía trên đường kẻ',
    btn_close: 'Đóng',
    btn_clear: 'Ký lại',
    btn_confirm: 'Hoàn tất chữ ký',
    btn_submit: 'Gửi Báo Cáo',
    btn_submitting: 'Đang gửi báo cáo...',
    btn_reset: 'Làm lại',
    reset_title: 'Làm lại từ đầu?',
    reset_desc: 'Toàn bộ thông tin vừa nhập sẽ bị xóa để bạn nhập lại.',
    btn_cancel: 'Hủy',
    btn_confirm_reset: 'Đồng ý xóa',
    reset_done: 'Biểu mẫu đã được làm lại từ đầu!',
    cam_off: 'Camera đang tắt',
    btn_cam_on: 'Bật Camera',
    btn_cam_off: 'Tắt Camera',
    cam_hint: 'Căn chỉnh đồng hồ xe hoặc hóa đơn vào khung',
    btn_retake: 'Chụp lại',
    btn_sample_img: 'Xem ảnh mẫu',
    btn_sample_ans: 'Xem câu trả lời mẫu',
    decree_footer: 'Thông tin khảo sát thu nhập được lưu trữ bảo mật nội bộ theo Nghị định 13.',
    history_title: 'Lịch Sử Các Lần Nộp',
    history_ph: 'Nhập số điện thoại để tra cứu...',
    history_query: 'Tra cứu',
    history_prompt: 'Nhập số điện thoại và bấm Tra cứu để xem các lần nộp trước.',
    history_empty: 'Chưa có lần nộp nào được ghi nhận cho số điện thoại này.',
    missing_required: 'Vui lòng trả lời câu hỏi bắt buộc: ',
    verdict_approved: 'BÁO CÁO HỢP LỆ! Khảo sát đã được ghi nhận thành công.',
    verdict_rejected: 'CẦN KIỂM TRA LẠI!',
    server_err: 'Lỗi kết nối máy chủ. Vui lòng thử lại sau.',
    status_approved: 'Đã duyệt',
    status_manual_review: 'Chờ kiểm tra',
    status_rejected: 'Cần kiểm tra lại',
    modal_approved_title: 'Báo Cáo Hợp Lệ!',
    modal_approved_desc: 'Khảo sát thu nhập thành công. Thông tin của bạn đã được ghi nhận.',
    modal_review_title: 'Chờ Người Thật Kiểm Tra',
    modal_review_desc: 'Hệ thống đã chuyển phiếu đến điều phối viên để kiểm tra thủ công.',
    modal_rejected_title: 'Chưa Đạt Yêu Cầu!',
    modal_rejected_desc: 'Vui lòng chụp lại ảnh rõ nét hoặc kiểm tra lại thông tin.',
    btn_open_telegram: 'Mở Telegram Bot',
    btn_view_history_web: 'Tra cứu lịch sử nộp trên web',
    btn_new_submission: 'Làm phiếu mới',
    locked_notice: "🔒 Vui lòng ký tên (Câu 1) và chọn 'Tôi đồng ý' (Câu 2) để mở toàn bộ câu hỏi khảo sát (Mã CTV, Xe, Doanh thu, Chụp ảnh...)"
  },
  en: {
    code: 'EN',
    gps_prompt: 'Allow location access to record survey location',
    gps_btn: 'Enable Location',
    gps_getting: 'Detecting location...',
    gps_acquiring: 'Getting location...',
    gps_acquired: '📍 Location acquired',
    gps_denied: 'Location permission denied. Please allow in settings.',
    gps_err: 'Cannot obtain location. Please try again.',
    mode_label: 'Form Mode:',
    mode_prod: 'Production',
    mode_test: 'Test Mode (Dev Storage)',
    btn_to_test: 'Switch to Test',
    btn_to_prod: 'Switch to Prod',
    bypass_label: 'Bypass image audit (Fast test data flow)',
    btn_history_nav: 'View History',
    sig_prompt: 'Tap here to sign on paper',
    sig_done: 'Signature recorded (Tap to resign)',
    sig_title: 'Driver Signature Sheet',
    sig_sub: 'Sign with your finger',
    sig_hint: 'Sign above the dashed line',
    btn_close: 'Close',
    btn_clear: 'Clear',
    btn_confirm: 'Confirm Signature',
    btn_submit: 'Submit Report',
    btn_submitting: 'Submitting report...',
    btn_reset: 'Reset',
    reset_title: 'Reset from scratch?',
    reset_desc: 'All entered information will be cleared.',
    btn_cancel: 'Cancel',
    btn_confirm_reset: 'Yes, Reset',
    reset_done: 'Form has been reset from scratch!',
    cam_off: 'Camera is off',
    btn_cam_on: 'Turn on Camera',
    btn_cam_off: 'Turn off Camera',
    cam_hint: 'Align odometer or receipt inside frame',
    btn_retake: 'Retake',
    btn_sample_img: 'View Sample Photo',
    btn_sample_ans: 'View Sample Answer',
    decree_footer: 'Survey data is encrypted and securely stored in compliance with Decree 13.',
    history_title: 'Submission History',
    history_ph: 'Enter phone number to look up...',
    history_query: 'Search',
    history_prompt: 'Enter phone number and click Search to view history.',
    history_empty: 'No submissions found for this phone number.',
    missing_required: 'Please answer required question: ',
    verdict_approved: 'REPORT APPROVED! Survey submitted successfully.',
    verdict_rejected: 'VERIFICATION FAILED!',
    server_err: 'Server connection error. Please try again later.',
    status_approved: 'Approved',
    status_manual_review: 'Manual Review',
    status_rejected: 'Rejected',
    modal_approved_title: 'Report Approved!',
    modal_approved_desc: 'Survey submitted successfully. Your submission has been recorded.',
    modal_review_title: 'Pending Manual Review',
    modal_review_desc: 'Your report has been forwarded to a coordinator for manual verification.',
    modal_rejected_title: 'Verification Failed!',
    modal_rejected_desc: 'Please retake a clear photo or verify your information.',
    btn_open_telegram: 'Open Telegram Bot',
    btn_view_history_web: 'View submission history on web',
    btn_new_submission: 'New Submission',
    locked_notice: "🔒 Please sign (Q1) and select 'Tôi đồng ý' (Q2) to unlock all survey questions (CTV Code, Vehicle, Revenue, Photos...)"
  }
};
