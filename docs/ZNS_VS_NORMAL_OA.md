# So Sánh Kỹ Thuật & Hiệu Quả Kinh Doanh: Zalo ZNS và Zalo Official Account (Normal OA)

> **Tài liệu đặc tả kiến trúc & chiến lược tối ưu chi phí truyền thông**  
> **Dự án:** Hệ thống Khảo sát & Xác minh Doanh thu Đội xe Công nghệ (GSM / VinFast / Xanh SM)  
> **Tài liệu liên quan:** `docs/COST_ESTIMATE_VND.md`, `docs/INTERNAL_SYSTEM_SPEC.md`  
> **Mục tiêu cốt lõi:** Cắt giảm chi phí nhắn tin từ **150.000.000 VNĐ/tháng** xuống gần **0 VNĐ/tháng** qua phễu chuyển đổi Hybrid (Giảm ~98% chi phí).

---

## 1. Bối Cảnh Nghiệp Vụ & Đặt Vấn Đề

Trong hệ thống xác minh khảo sát đội ngũ tài xế xe công nghệ (GSM / Xanh SM / VinFast), luồng thu thập dữ liệu diễn ra như sau:
1. Tài xế thực hiện khảo sát và **nhập số điện thoại (Phone Number)** vào biểu mẫu khảo sát trực tuyến (Web form / Kiosk / Landing page).
2. Hệ thống (`survey-pipeline`) thực hiện chuẩn hóa ảnh biên nhận qua AI Vision, kiểm định gian lận và ghi nhận kết quả.
3. Hệ thống cần phản hồi biên nhận xác nhận, thông báo kết quả duyệt thù lao, hướng dẫn sửa thông tin hoặc gửi lời cảm ơn đến tài xế.

### Bài toán chi phí ở quy mô lớn
Theo bảng dự toán tài chính tại `docs/COST_ESTIMATE_VND.md`:
- **Giai đoạn 1 (Pilot):** 300 phiếu/tháng $\rightarrow$ Chi phí gửi tin không đáng kể (~75.000 VNĐ).
- **Giai đoạn 2 (Go-Live):** 60.000 phiếu/tháng $\rightarrow$ Chi phí tin nhắn ZNS chiếm **15.000.000 VNĐ/tháng** (~90% tổng chi phí hạ tầng API).
- **Giai đoạn 3 (Toàn quốc - Scale-up):** 20.000 phiếu/ngày (**600.000 phiếu/tháng**) $\rightarrow$ Chi phí ZNS đơn thuần vọt lên tới **150.000.000 VNĐ/tháng** (chiếm **96.4%** toàn bộ ngân sách hạ tầng kỹ thuật $155.6\text{M VNĐ}$).

Việc phụ thuộc 100% vào ZNS khiến chi phí biến đổi tăng tuyến tính theo số lượng khảo sát, đè nặng lên biên lợi nhuận vận hành. Ngược lại, nếu chỉ dùng Normal OA thì không thể bắt đầu cuộc trò chuyện. Vì vậy, một bản so sánh toàn diện và kiến trúc phễu lai (Hybrid Funnel) là yêu cầu sống còn.

---

## 2. So Sánh Kiến Trúc Kỹ Thuật (Architectural Differences)

| Tiêu chí | Zalo ZNS (Zalo Notification Service) | Zalo Official Account Thường (Normal OA) |
| :--- | :--- | :--- |
| **Định danh mục tiêu (Target Identifier)** | **Số điện thoại (MSISDN / E.164)** ví dụ: `84912345678` | **Zalo User ID (UID)** riêng biệt được sinh ra theo từng OA |
| **Điều kiện tiên quyết để gửi tin** | **Không cần** tài xế phải quan tâm hay bấm Chat trước. Chỉ cần tài xế có tài khoản Zalo liên kết với số điện thoại | **Bắt buộc** tài xế phải bấm **Quan tâm OA (Follow)** hoặc chủ động gửi tin nhắn Chat tới OA trước đó |
| **Cửa sổ tương tác (Interaction Window)** | Gửi tin 1 chiều bất kỳ lúc nào có phát sinh giao dịch/sự kiện | Giới hạn tương tác miễn phí trong **48 giờ** kể từ tin nhắn cuối của người dùng (Customer Care Window) |
| **Chính sách nội dung (Content Policy)** | **Mẫu duyệt nghiêm ngặt (Strict Template):** Phải đăng ký trước với Zalo Cloud, không được đổi cấu trúc, cấm nội dung quảng cáo/spam | **Tự do (Free-form):** Hỗ trợ văn bản tự do, menu tương tác, ảnh, video, sticker, biểu mẫu khảo sát tương tác trực tiếp |
| **Giao thức tích hợp** | RESTful API (`POST https://business.openapi.zalo.me/message/template`) qua Access Token của Zalo Cloud Account (ZCA) | OpenAPI (`POST https://openapi.zalo.me/v3.0/oa/message/cs`) dựa trên OA Access Token / Webhook Events |
| **Khả năng nhận phản hồi 2 chiều** | Bị động; tài xế không thể trả lời trực tiếp trong luồng tin ZNS (chỉ có thể bấm nút CTA điều hướng) | 2 chiều hoàn chỉnh; hỗ trợ Chatbot kịch bản, AI tự động trả lời, hoặc chuyển giao cho tổng đài viên |

### 2.1 Định danh người dùng: Phone Number vs Zalo User ID
- **ZNS (Phone-based):** Sử dụng số điện thoại của tài xế thu thập từ form làm khóa chính. Zalo backend sẽ tự động ánh xạ số điện thoại sang tài khoản người dùng tương ứng để phân phối tin.
- **Normal OA (UID-based):** Zalo tuân thủ nghiêm ngặt chính sách bảo vệ quyền riêng tư (Privacy-by-Design). Hệ thống OA **không cho phép** doanh nghiệp tra cứu `user_id` từ số điện thoại thuần túy thông qua API. Mỗi người dùng khi tương tác với một OA sẽ được cấp một mã ẩn danh giả định (`user_id` scoped to OA). Nếu không có sự chủ động tương tác từ người dùng, doanh nghiệp hoàn toàn không có `user_id` để gửi tin.

### 2.2 Cơ chế kiểm duyệt nội dung
- **ZNS Template Registration:** Mỗi mẫu tin thông báo (xác nhận nộp phiếu, thông báo duyệt thù lao, cảnh báo ảnh mờ) phải được gửi duyệt kèm theo tài liệu chứng minh phát sinh giao dịch giữa tài xế và hệ thống (hợp đồng khảo sát/điều khoản sử dụng). Khi gửi API, payload chỉ được truyền các tham số biến (`<driver_name>`, `<ticket_code>`, `<survey_time>`, `<status>`). Bất kỳ nội dung khuyến mãi nào lồng ghép sẽ khiến template bị từ chối hoặc khóa tài khoản ZCA.
- **Normal OA:** Doanh nghiệp toàn quyền tùy biến nội dung văn bản, chèn nút bấm động (Interactive Action Buttons), tích hợp Webview Mini App hoặc kích hoạt kịch bản Chatbot hỏi đáp thông minh mà không cần qua khâu tiền duyệt từng câu chữ.

---

## 3. Cấu Trúc Chi Phí & Định Giá (Cost & Pricing Structure)

### 3.1 Đơn giá Zalo ZNS (Pay-per-use)
ZNS áp dụng mô hình trả phí theo lượt gửi thành công (Success-based Billing) thông qua tài khoản nạp tiền trước ZCA (Zalo Cloud Account):
- **Đơn giá tiêu chuẩn:** Khoảng **200 – 300 VNĐ / tin nhắn** (trung bình thực tế trong dự án tính toán: **250 VNĐ / tin**).
- **Phân loại cước ZNS:**
  - *ZNS Xác thực (OTP):* ~300 – 385 VNĐ/tin.
  - *ZNS Giao dịch / Chăm sóc khách hàng:* ~200 – 250 VNĐ/tin.
  - *ZNS Ưu đãi / Chăm sóc lại:* ~400 VNĐ/tin.
- **Quy mô chi phí theo tháng:**
  $$\text{Chi phí ZNS} = \text{Sản lượng khảo sát} \times 250\text{ VNĐ}$$
  - $60.000\text{ phiếu/tháng} \rightarrow 15.000.000\text{ VNĐ/tháng}$
  - $600.000\text{ phiếu/tháng} \rightarrow 150.000.000\text{ VNĐ/tháng}$ ($1.8\text{ tỷ VNĐ/năm}$)

### 3.2 Gói dịch vụ Normal Zalo OA (Subscription Tier)
Zalo OA doanh nghiệp vận hành theo mô hình thuê bao hàng tháng (Monthly Subscription) kết hợp hạn mức tin nhắn tích hợp sẵn:

| Gói cước Zalo OA | Chi phí thuê bao / tháng | Hạn mức tin nhắn Tư vấn (CS) | Phí gửi tin vượt hạn mức | Tính năng hỗ trợ nổi bật |
| :--- | :--- | :--- | :--- | :--- |
| **Gói Cơ bản (Miễn phí)** | **0 VNĐ** | Giới hạn 20 tin/tháng | Không hỗ trợ mở rộng | Chỉ dùng kiểm thử kỹ thuật nội bộ |
| **Gói Dùng thử (Trial)** | 10.000 VNĐ / 30 ngày | 50 tin nhắn | Không khả dụng | Trải nghiệm tính năng có hạn |
| **Gói Nâng cao (Standard)** | **399.000 VNĐ / tháng** | **2.000 tin Tư vấn / tháng** | Hỗ trợ mua gói tin ZNS/CS phụ | Tự động trả lời, phân nhóm thẻ tag, Open API đầy đủ |
| **Gói Premium (Doanh nghiệp lớn)** | **3.999.000 VNĐ / tháng** | **10.000 tin Tư vấn / tháng** | Mức chiết khấu cao cho lượt gửi bổ sung | Quản trị viên không giới hạn, đường truyền API ưu tiên cao |

*Ghi chú quan trọng về tương tác 48h của OA:* Trong vòng 48 giờ kể từ khi tài xế gửi tin nhắn hoặc bấm vào menu tương tác trên OA, doanh nghiệp gửi tin phản hồi qua API hoàn toàn **0 VNĐ** (miễn phí trong quota tương tác của phiên).

---

## 4. "The Catch": Vì Sao Normal OA Không Thể Nhắn Tin Trực Tiếp Từ Số Điện Thoại?

Nhiều nhà phát triển thường đặt câu hỏi: *"Hệ thống đã có số điện thoại tài xế từ form khảo sát, tại sao không gọi API của Normal OA để gửi tin nhắn chào mừng ngay lập tức nhằm tiết kiệm chi phí?"*

Đây là rào cản cốt lõi trong chính sách chống tin rác (Anti-Spam & User Privacy Policy) của VNG / Zalo:

```
[Form Khảo sát] 
   └── Thu được: SĐT "0912345678"
         │
         ├──❌ Gửi qua Normal OA API: Thất bại!
         │     Nguyên nhân: OA API không nhận SĐT, chỉ nhận Zalo User ID (UID).
         │     Không có API reverse lookup từ SĐT ra UID để tránh thu thập dữ liệu trái phép.
         │
         └──✅ Gửi qua ZNS API: Thành công!
               Cơ chế: ZNS là kênh viễn thông chính thống đã ký quỹ ZCA.
               Zalo engine tự tìm account tương ứng với SĐT để đẩy thông báo.
```

### Chi tiết các giới hạn kỹ thuật:
1. **Thiếu API Tra cứu SĐT $\rightarrow$ UID:** Zalo tuyệt đối không cung cấp endpoint kiểu `GET /v3.0/oa/user/by-phone?phone=...`. Doanh nghiệp không thể suy luận ra `user_id` từ số điện thoại của tài xế một cách độc lập.
2. **Quy tắc bắt tay chủ động (Opt-in Requirement):** Người dùng phải là người khởi tạo phiên tương tác (chủ động bấm "Quan tâm", gửi tin nhắn, hoặc quét mã QR dẫn vào OA).
3. **Cơ chế Cookie/Session độc lập:** Form khảo sát chạy trên trình duyệt web độc lập (Chrome, Safari, hoặc In-app browser của ứng dụng ngoài) không có session đăng nhập Zalo để tự động chia sẻ UID nếu không qua cổng cấp quyền Zalo Login SDK (vốn đòi hỏi sự đồng thuận phức tạp từ người dùng).

Do đó: **Không thể loại bỏ ZNS hoàn toàn trong lần chạm đầu tiên nếu kênh tiếp nhận duy nhất là số điện thoại từ web form.**

---

## 5. Chiến Lược Phễu Lai Tiết Kiệm 98% Chi Phí (The Hybrid Funnel Strategy)

Để giải quyết bài toán mâu thuẫn giữa tính tiện dụng của Số điện thoại và bài toán chi phí khổng lồ của ZNS, giải pháp tối ưu là **Mô hình Phễu Lai Kết Hợp (Hybrid Funnel)**: *Dùng 1 tin ZNS duy nhất làm "mồi câu" (bridge hook) để chuyển đổi tài xế về hệ sinh thái 0 VNĐ (Normal OA & Telegram Bot).*

### 5.1 Kiến trúc luồng tương tác (Interaction Flow)

```
                    ┌────────────────────────────────────────────────────────┐
                    │ BƯỚC 1: Tài xế điền SĐT vào Form Khảo sát (Web form)    │
                    └──────────────────────────┬─────────────────────────────┘
                                               │
                                               ▼
                    ┌────────────────────────────────────────────────────────┐
                    │ BƯỚC 2: Backend ghi nhận phiếu, gửi 01 tin Zalo ZNS    │
                    │         Đơn giá: ~250 VNĐ (Chi phí đầu tư chuyển đổi)  │
                    │                                                        │
                    │ Nội dung ZNS:                                          │
                    │ "Xác nhận tiếp nhận phiếu khảo sát #T123. Vui lòng bấm │
                    │  nút bên dưới để theo dõi trạng thái duyệt thù lao."   │
                    │                                                        │
                    │ [ Nút CTA: 🔔 QUAN TÂM OA & XEM KẾT QUẢ DUYỆT ]       │
                    └──────────────────────────┬─────────────────────────────┘
                                               │
                               (Tài xế bấm nút CTA trên Zalo)
                                               │
                                               ▼
                    ┌────────────────────────────────────────────────────────┐
                    │ BƯỚC 3: Mở Zalo OA ──> Tài xế bấm "QUAN TÂM" (Follow)   │
                    └──────────────────────────┬─────────────────────────────┘
                                               │
                       Webhook: event "follow" / "user_send_text"
                                               │
                                               ▼
                    ┌────────────────────────────────────────────────────────┐
                    │ BƯỚC 4: Webhook Handler liên kết SĐT ──> Zalo UID      │
                    │         Lưu vào Database (`driver_identities`)         │
                    └──────────────────────────┬─────────────────────────────┘
                                               │
                                               ▼
         ┌────────────────────────────────────────────────────────────────────────┐
         │ BƯỚC 5: TẤT CẢ CÁC THÔNG BÁO TƯƠNG LAI                                  │
         │ - Khảo sát các tuần tiếp theo                                          │
         │ - Thông báo đối soát thù lao định kỳ                                   │
         │ - Nhắc nhở ca trực, khiếu nại, cảnh báo                                │
         │                                                                        │
         │ ══════════════════════════════════════════════════════════════════════ │
         │ Gửi qua: Zalo Normal OA (0 VNĐ) HOẶC Telegram Bot API (0 VNĐ)          │
         └────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Cơ chế liên kết danh tính (Phone-to-UID Binding Mechanism)

Làm thế nào để hệ thống biết tài xế vừa bấm "Quan tâm" trên OA chính là người vừa nhập số điện thoại `0912345678`?

Có 3 giải pháp kỹ thuật để thực hiện việc liên kết:

#### Cách 1: Sử dụng Deep-link kèm Payload bí mật (Khuyến nghị triển khai)
1. Trong tin nhắn ZNS, nút bấm CTA có dạng link dẫn vào OA:  
   `https://zalo.me/<OA_ID>?ref_code=<HMAC_TOKEN_OR_SESSION_ID>`
2. Khi tài xế bấm nút, Zalo mở cửa sổ chat OA và tự động gửi/gợi ý gửi kèm mã `ref_code` hoặc kích hoạt Webhook chào mừng.
3. Webhook của OA nhận sự kiện tương tác kèm tham số giới thiệu, trích xuất `ref_code` để tra cứu trong bảng cache Redis/PostgreSQL $\rightarrow$ Ánh xạ thành công `phone_number` $\leftrightarrow$ `zalo_user_id`.

#### Cách 2: Yêu cầu chia sẻ thông tin liên lạc (Zalo Request User Info / Phone Token)
1. Khi tài xế bấm Quan tâm OA, OA gửi ngay 1 tin nhắn chào mừng tự động kèm nút:  
   `[ 📱 Đồng bộ số điện thoại nhận thưởng ]`.
2. Tài xế bấm nút $\rightarrow$ Zalo hiển thị popup chuẩn hệ thống cho phép chia sẻ số điện thoại tài khoản Zalo.
3. Backend nhận Access Token từ Zalo, gọi API `https://graph.zalo.me/v2.0/me/info` trích xuất số điện thoại thực tế $\rightarrow$ Cập nhật `zalo_user_id` vào hồ sơ tài xế.

#### Cách 3: Điều hướng kép sang Telegram Bot (Kênh hoàn toàn miễn phí trọn đời)
1. Bên cạnh nút Quan tâm OA, tin ZNS hoặc tin nhắn đầu tiên của OA cung cấp link bot Telegram:  
   `https://t.me/gsm_survey_bot?start=bind_<driver_id>`.
2. Tài xế GSM vốn đã rất quen thuộc với Telegram trong việc nhận điều phối đội xe $\rightarrow$ Chuyển hoàn toàn kênh thông báo sang Telegram Bot API với chi phí **0 VNĐ tuyệt đối**, không giới hạn số lượng và độ dài tin nhắn.

---

## 6. Phân Tích Hiệu Quả Tài Chính (Financial Impact & ROI)

### Bảng so sánh chi phí vận hành ở Giai đoạn 3 (600.000 phiếu/tháng, 1.000–2.000 tài xế)

Giả định nghiệp vụ thực tế đối với đội xe công nghệ:
- Tỷ lệ tài xế quay lại làm khảo sát định kỳ (Retention rate): **85%**.
- Tỷ lệ tài xế mới gia nhập mỗi tháng cần kích hoạt ZNS lần đầu: **15%** (~90.000 lượt ZNS/tháng cho tài xế mới hoặc tài xế chưa liên kết).
- Số lượng khảo sát lặp lại từ các tài xế đã liên kết OA / Telegram: **510.000 phiếu/tháng**.

| Hạng mục chi phí | Phương án 1: 100% ZNS (Nguyên bản) | Phương án 2: 100% Normal OA (Phi thực tế) | Phương án 3: Mô hình Phễu Lai (Hybrid Funnel) |
| :--- | :--- | :--- | :--- |
| **Tính khả thi kỹ thuật** | Khả thi 100% | **Không thể thực hiện** (Lỗi thiếu Zalo UID) | **Khả thi 100%** (Tối ưu nhất) |
| **Chi phí gửi tin ZNS** | 600.000 tin $\times$ 250đ = **150.000.000 VNĐ** | 0 VNĐ | 90.000 tin $\times$ 250đ = **22.500.000 VNĐ** *(hoặc giảm xuống dưới 5M VNĐ khi tập tài xế ổn định)* |
| **Chi phí thuê bao Zalo OA** | 0 VNĐ (Không cần duy trì OA) | 3.999.000 VNĐ (Gói Premium) | 399.000 VNĐ / tháng (Gói Nâng cao) |
| **Chi phí tin nhắn gửi qua OA/Tele** | 0 VNĐ | 0 VNĐ | **0 VNĐ** (Miễn phí qua OA / Telegram) |
| **Tổng chi phí truyền thông / tháng** | **150.000.000 VNĐ** | N/A (Bế tắc) | **22.899.000 VNĐ** (Tháng đầu) $\rightarrow$ **~3.500.000 VNĐ** (Khi đã bind >95% tài xế) |
| **Ngân sách tiết kiệm được / tháng** | **0 VNĐ** (Mốc cơ sở) | Không khả dụng | **127.101.000 VNĐ – 146.500.000 VNĐ / tháng** |
| **Tỷ lệ cắt giảm chi phí** | **0%** | N/A | **Giảm 84.7% (Giai đoạn đầu) $\rightarrow$ Giảm 97.7% (~98%)** |

> **Tổng kết lợi nhuận:** Việc triển khai kiến trúc Hybrid Funnel giúp dự án tiết kiệm được **từ 1.5 đến 1.75 tỷ VNĐ mỗi năm**, giải phóng hoàn toàn áp lực tài chính của hạng mục API viễn thông.

---

## 7. Bảng Ma Trận So Sánh Toàn Diện (Comparative Matrix)

| Đặc tính / Chỉ số | Zalo ZNS | Normal Zalo OA | Mô hình Phễu Lai (Hybrid Funnel) |
| :--- | :--- | :--- | :--- |
| **Bản chất dịch vụ** | Tin nhắn thông báo đẩy (Push Transactional SMS) | Kênh giao tiếp thương hiệu & Chăm sóc 2 chiều | Kết hợp chuyển đổi 1 chiều sang 2 chiều |
| **Độ phủ tiếp cận (Reach)** | Toàn bộ người dùng có tài khoản Zalo liên kết SĐT | Chỉ người dùng đã bấm Quan tâm OA | Tối đa hóa: Tiếp cận bằng SĐT, giữ chân bằng OA |
| **Tốc độ gửi & Độ trễ** | Rất nhanh (< 3 giây, độ ưu tiên viễn thông) | Rất nhanh qua Webhook / OpenAPI | Nhanh tức thời |
| **Tỷ lệ mở tin (Open Rate)** | > 92% (Hiển thị dạng thông báo ưu tiên Zalo) | 70% – 85% (Nằm trong tab Official Account) | Duy trì > 90% (Do tài xế cần theo dõi tiền thưởng) |
| **Mô hình tính giá** | Trả tiền theo từng tin gửi thành công (~250đ/tin) | Thuê bao cố định/tháng + Quota tin miễn phí | Chi phí cận biên tiến dần về 0 VNĐ |
| **Khả năng tương tác người dùng** | Kém (Chỉ có nút bấm điều hướng Web/OA) | Xuất sắc (Chat 2 chiều, Menu động, Bot AI) | Toàn diện: Kéo tài xế vào luồng tương tác sâu |
| **Mức độ phụ thuộc chi phí theo sản lượng** | Tuyến tính $O(N)$ — Càng đông càng tốn kém | Cố định $O(1)$ — Bền vững khi mở rộng quy mô | Tối ưu $O(\Delta N)$ — Chỉ tốn phí cho tài xế mới |
| **Rủi ro vi phạm chính sách Zalo** | Cao nếu lồng ghép nội dung tiếp thị, mời chào | Rất thấp (Tài xế đã tự nguyện bấm Quan tâm) | Rất an toàn, tuân thủ đúng chuẩn Dev Guidelines |

---

## 8. Hướng Dẫn Kỹ Thuật Triển Khai (Implementation Guidelines)

### 8.1 Thiết kế Schema Cơ Sở Dữ Liệu (`PostgreSQL`)

Để hỗ trợ ánh xạ danh tính và định tuyến tin nhắn thông minh, hệ thống cần bổ sung các bảng sau trong cơ sở dữ liệu:

```sql
-- Bảng quản lý định danh đa kênh của tài xế
CREATE TABLE driver_identities (
    driver_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_number VARCHAR(15) UNIQUE NOT NULL,      -- Định danh từ form khảo sát (E.164: +84...)
    zalo_user_id VARCHAR(64) UNIQUE,               -- Scoped User ID do Zalo OA cấp khi tương tác
    telegram_chat_id BIGINT UNIQUE,                -- Telegram Chat ID (nếu tài xế dùng Bot Tele)
    full_name VARCHAR(100),
    is_oa_follower BOOLEAN DEFAULT FALSE,
    last_oa_interaction_at TIMESTAMPTZ,            -- Để xác định cửa sổ 48 giờ
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index hỗ trợ tra cứu định tuyến gửi tin tốc độ cao
CREATE INDEX idx_driver_phone ON driver_identities(phone_number);
CREATE INDEX idx_driver_zalo_uid ON driver_identities(zalo_user_id);
CREATE INDEX idx_driver_oa_active ON driver_identities(is_oa_follower, last_oa_interaction_at);
```

### 8.2 Thuật toán định tuyến gửi tin nhắn thông minh (Smart Dispatcher Flow)

Khi một phiếu khảo sát được phê duyệt hoặc cần gửi biên nhận, `NotificationDispatcher` sẽ chọn kênh theo thứ tự ưu tiên chi phí thấp nhất:

```
[Phát sinh yêu cầu gửi thông báo tới tài xế theo SĐT]
                        │
                        ▼
            Tra cứu `driver_identities`
                        │
       ┌────────────────┴────────────────┐
       ▼                                 ▼
ĐÃ CÓ `telegram_chat_id`?        CHƯA CÓ Telegram
       │                                 │
   [CÓ]│                                 ▼
       │                     ĐÃ CÓ `zalo_user_id` VÀ
       │                     `last_interaction` < 48h?
       │                                 │
       │                         ┌───────┴───────┐
       │                     [CÓ]│               │[KHÔNG]
       │                         │               │
       ▼                         ▼               ▼
GỬI TELEGRAM BOT API     GỬI ZALO NORMAL OA     GỬI ZALO ZNS API
(Chi phí: 0 VNĐ)         (Chi phí: 0 VNĐ)       (Chi phí: ~250 VNĐ)
                                                 Kèm CTA Bind Link!
```

### 8.3 Mẫu xử lý Webhook tiếp nhận sự kiện Zalo OA (`FastAPI`)

```python
from fastapi import APIRouter, Request, BackgroundTasks
import hmac
import hashlib

router = APIRouter(prefix="/webhook/zalo", tags=["Zalo OA Webhook"])

@router.post("/events")
async def handle_zalo_oa_events(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    event_name = payload.get("event_name")
    sender_id = payload.get("sender", {}).get("id")
    
    if event_name == "follow":
        # Tài xế vừa bấm quan tâm OA
        # Trích xuất ref_code (nếu có từ URL click)
        ref_code = payload.get("info", {}).get("ref_code")
        background_tasks.add_task(bind_driver_by_ref_code, sender_id, ref_code)
        
    elif event_name == "user_send_text":
        text_message = payload.get("message", {}).get("text", "").strip()
        background_tasks.add_task(process_driver_chat_message, sender_id, text_message)
        
    return {"status": "ok", "error": 0, "message": "Success"}

async def bind_driver_by_ref_code(zalo_user_id: str, ref_code: str):
    """
    Ánh xạ session form khảo sát tương ứng với mã ref_code sang zalo_user_id
    để từ sau không cần gửi ZNS tốn phí nữa.
    """
    if not ref_code:
        return
    # Cập nhật PostgreSQL: UPDATE driver_identities SET zalo_user_id = ... WHERE ref_token = ...
```

---

## 9. Khuyến Nghị Lộ Trình Triển Khai Thực Tế

1. **Giai đoạn Pilot (Tháng 1):** Tiếp tục dùng ZNS hoặc Telegram Bot nội bộ để xác lập chuẩn xác thực dữ liệu. Chi phí ở mức nhỏ không đáng kể.
2. **Giai đoạn Go-Live (Tháng 2 - Tháng 3):**
   - Đăng ký tài khoản Zalo OA doanh nghiệp đã tích xanh (Business Verified).
   - Đăng ký 01 mẫu ZNS tiêu chuẩn: *"Xác nhận tiếp nhận phiếu khảo sát GSM #TicketID. Nhấn Quan tâm OA để nhận thông báo duyệt thù lao tức thì."*
   - Cài đặt Webhook liên kết tự động `phone_number` $\leftrightarrow$ `zalo_user_id`.
3. **Giai đoạn Tối ưu hóa (Tháng 4 trở đi):**
   - Đẩy mạnh tỷ lệ chuyển đổi sang Telegram Bot bằng cách tặng ưu tiên xử lý nhanh cho tài xế kết nối Bot.
   - Giảm tỷ lệ gửi ZNS xuống < 5% tổng sản lượng phiếu, đạt mục tiêu ngân sách viễn thông tiệm cận **0 VNĐ/tháng**.
