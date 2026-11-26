# Mẫu Hướng dẫn Chiến dịch Khảo sát (Campaign Playbook Template)

> **Mục đích**: Nhân bản tệp này cho từng chiến dịch cụ thể (ví dụ `docs/campaigns/TB_GSM_2026_Nov.md`). Tài liệu nền tảng chung nằm tại [HUONG_DAN_CTV.md](HUONG_DAN_CTV.md) và [HUONG_DAN_QUAN_TRI.md](HUONG_DAN_QUAN_TRI.md); tài liệu chiến dịch chỉ ghi rõ quy tắc riêng của chiến dịch đó.

---

## 1. Thông tin chung chiến dịch

| Mục | Giá trị |
|---|---|
| **Tên chiến dịch** | *(Ví dụ: Khảo sát hoạt động tài xế công nghệ GSM - Tháng 11/2026)* |
| **Mã biểu mẫu (`form_id`)** | *(Ví dụ: `TB_GSM_2026_Nov_Test`)* |
| **Thời gian chạy** | *(Từ ngày ... đến ngày ... hoặc đến khi đủ giới hạn)* |
| **Trạng thái truy cập (`access_control`)** | `public` *(chỉ CTV được phân công)* / `private` *(nháp, chỉ nhân sự xem)* |
| **Giới hạn phiếu duyệt (`response_limit`)** | *(Số lượng `APPROVED` tối đa, hoặc `null`/`0` = không giới hạn)* |
| **Bắt buộc duyệt thủ công (`require_manual_review`)** | `true` *(khuyến nghị)* / `false` *(tự động duyệt khi đạt AI & module)* |
| **Theo dõi mã CTV (`ctv_ref_enabled`)** | `true` *(mặc định)* |
| **Đích nhận kết quả Telegram (`tg_response_source`)** | `ctv` / `question` *(ghi rõ `tg_response_question_id`)* / `none` |
| **Nhãn (`tags`)** | *(Ví dụ: `gsm`, `hcm`, `nov_2026` - dùng cho mục tiêu theo nhãn)* |

---

## 2. Đối tượng khảo sát & Tiêu chí hợp lệ

- **Đối tượng tài xế mục tiêu**: *(Nêu rõ hãng xe, loại xe `CAR`/`BIKE`, khu vực địa lý, điều kiện hoạt động tối thiểu).*
- **Quy tắc lặp tài xế**:
  - Cùng một tài xế có thể tham gia các chiến dịch khác nhau.
  - Nếu phiếu trước bị `REJECTED`, tài xế được nộp lại cho chiến dịch này.
  - *(Ghi rõ thêm nếu chiến dịch có giới hạn nghiệp vụ riêng, ví dụ mỗi tài xế chỉ tính 1 phiếu `APPROVED` trong chiến dịch).*
- **Yêu cầu bắt buộc chung**:
  - Bật định vị GPS hợp lệ tại thời điểm nộp phiếu (hệ thống chặn nếu thiếu GPS).
  - Tài xế đồng ý Nghị định 13 (`"Tôi đồng ý"`) và ký xác nhận; phiếu chọn `"Tôi không đồng ý"` không tính `APPROVED`, không tính mục tiêu và không tính thù lao.

---

## 3. Yêu cầu chụp ảnh & Kiểm tra tự động

### 3.1 Danh sách ô ảnh (`image_slots`)
| Slot | Tên hiển thị | Vai trò (`role`) | Bắt buộc | Tiêu chí đạt chuẩn | Lý do hay bị từ chối |
|---|---|---|---|---|---|
| 0 | *(Ví dụ: Màn hình hồ sơ ứng dụng)* | `app_profile` | Có | *(Rõ tên, trạng thái tài khoản, không bị lóa)* | *(Ảnh mờ, cắt góc, chụp lại ảnh cũ)* |
| 1 | *(Ví dụ: Màn hình thu nhập tuần)* | `app_income` | Có | *(Rõ doanh thu, số chuyến, đúng tuần yêu cầu)* | *(Thiếu số liệu, sai tuần)* |

### 3.2 Cấu hình kiểm tra ảnh (`image_checks`) & Module (`modules`)
- **Kiểm tra ảnh**:
  - Độ nét (`enable_blur_check`, `blur_threshold`): *(Ví dụ: bật, ngưỡng `40.0`)*
  - Chống gian lận / trùng ảnh (`enable_anti_fraud`, `require_unique`, `enable_ai_generated_check`): *(Bật)*
  - Nhận diện AI / Object (`enable_ai_vision`, `enable_object_detection`): *(Ghi rõ bật/tắt)*
- **Module kiểm tra dữ liệu (`reject_on_false = true`)**:
  - `phone_format`: số điện thoại `0` + 9 số hoặc `+84` + 9 số.
  - *(Liệt kê thêm các module đặc thù của chiến dịch cùng `reject_message` tương ứng).*

---

## 4. Phân công CTV, Mục tiêu & Quyết toán

- **Phân công CTV (`/admin/ctvs`)**:
  - Mặc định khi tạo biểu mẫu mới, toàn bộ CTV đang hoạt động (không phải tài khoản `*TEST*`) được tự động phân công.
  - *(Ghi rõ nếu cần gỡ/chỉ định nhóm CTV cụ thể theo khu vực).*
  - Thu hồi phân công sẽ chặn mở link và chặn nộp phiếu ngay lập tức (tính theo thời điểm gửi bài).
- **Mục tiêu CTV (`ctv_quotas`)**:
  - *(Ghi rõ mục tiêu theo biểu mẫu / nhãn / kỳ cuốn chiếu; lưu ý mục tiêu chỉ để theo dõi và chỉ tính phiếu `APPROVED`).*
- **Định mức & Quyết toán**:
  - *(Ghi rõ đơn giá hoặc quy tắc tính thù lao theo dữ liệu phiếu của chiến dịch này).*
  - Thanh toán thực hiện quyết toán ngoại tuyến (offline) dựa trên dữ liệu đã duyệt; `superadmin` cập nhật trạng thái hoàn tất hoặc thu hồi trên `/admin/payouts` (mọi thay đổi đều ghi nhận trong `/admin/audit`).

---

## 5. Lịch xuất Google Drive & Đầu mối hỗ trợ

- **Cấu hình xuất (`/admin/exports`)**:
  - Thư mục khách hàng: *(Tên folder khách hàng)*
  - Trạng thái xuất: *(Mặc định chỉ `APPROVED` cho khách hàng)*
  - Tần suất xuất: *(Hàng ngày / Hàng tuần / Theo yêu cầu)*
- **Đầu mối vận hành**:
  - Quản lý chiến dịch (`manager`): *(Tên / kênh liên hệ)*
  - Hỗ trợ CTV & duyệt phiếu: Bot Telegram chính thức + nhóm quản trị Telegram
  - Xử lý phúc khảo: `/admin/appeals` hoặc bot Telegram (tương đương nhau)
  - Sự cố tài khoản / thu hồi phiếu / kiểm toán: `superadmin`
