# Dự Toán Chi Phí Vận Hành & Nhân Sự Hệ Thống Khảo Sát Tự Động
## Survey Verification Pipeline — Financial & Staffing Breakdown

- **Tỷ giá quy đổi tiêu chuẩn:** $1\text{ USD} \approx 25.400\text{ VNĐ}$
- **Hệ thống mục tiêu:** Đường ống xác minh biên nhận / doanh thu khảo sát đội xe công nghệ (GSM / Xanh SM)
- **Tài liệu tham chiếu kiến trúc:** `docs/INTERNAL_SYSTEM_SPEC.md`

---

## 1. Giới Thiệu & Mô Hình 3 Giai Đoạn Vận Hành

Đường ống xác minh khảo sát (`survey-pipeline`) được thiết kế theo tiêu chí tối ưu hóa chi phí với kiến trúc phi máy chủ kết hợp micro-services tinh gọn (FastAPI, Caddy, Cloudflare R2, Telegram Bot API và mô hình AI Vision kép: DeepSeek Flash + Gemini 3.5 Flash no-reasoning).

Mô hình tài chính được phân tích chi tiết qua 3 giai đoạn mở rộng quy mô:

1. **Giai đoạn 1 (Hiện tại - Pilot / Thử nghiệm):**
   - Sản lượng: **300 phiếu/tháng** (~10 phiếu/ngày).
   - Quy mô đội ngũ: **5 tài xế** (định mức chuẩn: ~2 phiếu/tài xế/ngày tương ứng ca đầu và ca cuối kiểm tra phương tiện/doanh thu).
   - Mục tiêu: Chạy kiểm thử luồng xác thực hình ảnh, chống giả mạo, cân chỉnh ngưỡng mờ và đối soát doanh thu.
2. **Giai đoạn 2 (Mục tiêu ban đầu - Production Go-Live):**
   - Sản lượng: **2.000 phiếu/ngày** (**~60.000 phiếu/tháng**).
   - Quy mô đội ngũ: **1.000 tài xế hoạt động** (chuẩn hóa tỷ lệ: 2.000 phiếu/ngày ÷ 2 phiếu/tài xế/ngày = 1.000 tài xế).
   - Mục tiêu: Triển khai toàn diện trên thị trường thực tế với luồng duyệt tự động > 90%, tích hợp quản trị qua Lark Base và hạ tầng viễn thông/ZNS minh bạch.
3. **Giai đoạn 3 (Quy mô 10x - Scale-Up Toàn quốc):**
   - Sản lượng: **20.000 phiếu/ngày** (**~600.000 phiếu/tháng**).
   - Quy mô đội ngũ: **10.000 tài xế hoạt động** (20.000 phiếu/ngày ÷ 2 phiếu/tài xế/ngày = 10.000 tài xế).
   - Mục tiêu: Vận hành ở quy mô lớn trên nhiều tỉnh thành, tối ưu hóa độ trễ, lưu trữ vĩnh viễn hình ảnh tối ưu trên Cloudflare R2 với $0 băng thông tải ra.
---

## 2. Chi Tiết Chi Phí Hạ Tầng & Dịch Vụ API (Itemized Infrastructure Costs)

### 2.1 Định mức đơn giá và nguyên tắc tính toán

| Hạng mục hạ tầng / API | Đơn giá & Chính sách nhà cung cấp | Cơ chế tối ưu của Pipeline | Chi phí định mức trên 1 phiếu |
| :--- | :--- | :--- | :--- |
| **Máy chủ Cloud** | DigitalOcean SGP1 (2 vCPU, 4GB RAM: $24/tháng; 4 vCPU, 8GB RAM: $48/tháng) hoặc Hetzner Singapore | Caddy reverse-proxy + Uvicorn 4 workers; libjpeg-turbo C draft decode giảm 85% RAM. | Cố định theo tháng, chia đều theo quy mô |
| **Cơ sở dữ liệu** | Supabase Pro ($25/tháng) hoặc DigitalOcean Managed PostgreSQL ($15 – $60/tháng) | Transaction Outbox Pattern (`SKIP LOCKED`), partial index chống trùng, lưu trữ bitwise dHash. | Cố định theo tháng |
| **Lưu trữ ảnh Cloudflare R2** | Lưu trữ: $0.015/GB/tháng (miễn phí 10GB đầu). Ghi Class A: $4.50/1M ops (miễn phí 1M ops/tháng). **Băng thông tải ra: $0.00 (Hoàn toàn miễn phí)** | Ảnh được nén sang định dạng WebP (quality 82, 720px short edge) chỉ nặng ~49.4 KB/ảnh (tiết kiệm 93.9% dung lượng). | ~$0.000006 USD (~0.15 VNĐ/ảnh) |
| **AI Vision API** | DeepSeek Flash ($0.14/1M input tokens, $0.28/1M output tokens) kết hợp Gemini 3.5 Flash no-reasoning backup ($0.10 input, $0.40 output) | Chỉ trích xuất dữ liệu thô (extract-then-evaluate), tắt thinking/reasoning (`thinking_budget=0`), giới hạn payload ~1.000 tokens input + ~100 tokens JSON output. | ~$0.00018 USD (~4.57 VNĐ/phiếu) |
| **Zalo ZNS** | Tin nhắn Zalo Notification Service chính thức: ~250 VNĐ/tin (~$0.00984 USD/tin) | Gửi tự động thông báo cảm ơn và mã biên nhận ngay sau khi phiếu được hệ thống tiếp nhận/phê duyệt. | 250 VNĐ/phiếu |
| **Telegram Bot API** | Chính thức từ Telegram Bot Platform: **0 VNĐ ($0.00 USD)** | Webhook secret token verification, tương tác 1-1 riêng tư với tài xế và chuyển tiếp nhóm điều hành. | 0 VNĐ |
| **Tên miền & SSL Caddy** | Tên miền quốc tế: ~$12/năm (~1 USD/tháng = 25.400 VNĐ/tháng). Chứng chỉ SSL Caddy: **0 VNĐ ($0.00 USD)** | Caddy tự động cấp phát và gia hạn chứng chỉ Let's Encrypt / ZeroSSL miễn phí trọn đời. | Cố định ~25.400 VNĐ/tháng |
| **Bytedance Lark Base** | Gói tiêu chuẩn (Standard Tier) cho doanh nghiệp: **0 VNĐ ($0.00 USD)** | Đồng bộ dữ liệu quản trị tự động qua Open API (Admin Grid, QC Kanban, Telesales CRM). | 0 VNĐ |
| **Xuất dữ liệu Google Drive** | Google Drive API qua Service Account: **0 VNĐ ($0.00 USD)** | Outbox worker tự động tải ảnh WebP và xuất báo cáo ngày sang Google Sheets dùng chung cho đối tác. | 0 VNĐ |

---

### 2.2 Bảng chi phí hạ tầng & API chi tiết theo 3 giai đoạn

#### Giai đoạn 1: Hiện tại (300 phiếu/tháng, 5 tài xế)
*Quy mô thử nghiệm kỹ thuật — Tận dụng tối đa các gói Free Tier.*

| STT | Dịch vụ | Quy cách / Gói sử dụng | Chi phí / Tháng (VNĐ) | Chi phí / Tháng (USD) | Ghi chú kỹ thuật |
| :---: | :--- | :--- | :---: | :---: | :--- |
| 1 | **Máy chủ Cloud** | Droplet 1–2 vCPU, 2–4GB RAM (Hetzner / DO) | 304.800 VNĐ | $12.00 | Đủ tải chạy toàn bộ Docker stack đơn lẻ |
| 2 | **Cơ sở dữ liệu** | PostgreSQL tích hợp trên VPS hoặc Supabase Free | 0 VNĐ | $0.00 | Nằm trọn trong gói miễn phí 500MB |
| 3 | **Cloudflare R2** | Lưu trữ ~15 MB WebP, 300 lượt tải lên | 0 VNĐ | $0.00 | Dưới ngưỡng miễn phí (10GB & 1M ops) |
| 4 | **AI Vision API** | 300 lượt gọi (DeepSeek Flash + Gemini Flash) | 1.270 VNĐ | $0.05 | Thực tế được bù bởi hạn mức tín dụng miễn phí |
| 5 | **Zalo ZNS** | 300 tin nhắn cảm ơn chính thức (250đ/tin) | 75.000 VNĐ | $2.95 | Gửi trực tiếp tới số điện thoại tài xế |
| 6 | **Telegram Bot API** | Telegram Bot API Webhook | 0 VNĐ | $0.00 | Không giới hạn tin nhắn |
| 7 | **Tên miền & SSL** | Domain .com/.vn (~$12/năm) + Caddy TLS 1.3 | 25.400 VNĐ | $1.00 | SSL tự động kích hoạt bởi Caddy |
| 8 | **Lark Base** | Workspace Bytedance Lark Cloud | 0 VNĐ | $0.00 | Gói tiêu chuẩn tích hợp Webhook |
| 9 | **Google Drive** | Service Account Drive Export | 0 VNĐ | $0.00 | Lưu trữ báo cáo đối soát |
| **TỔNG** | **Hạ tầng & API Giai đoạn 1** | | **406.470 VNĐ** | **$16.00** | **4.877.640 VNĐ / năm ($192.03)** |

---

#### Giai đoạn 2: Mục tiêu ban đầu (60.000 phiếu/tháng, ~1.000 tài xế hoạt động)
*Vận hành thương mại ổn định (2.000 phiếu/ngày ÷ 2 phiếu/tài xế/ngày = 1.000 tài xế).*

| STT | Dịch vụ | Quy cách / Gói thực tế | Chi phí / Tháng (VNĐ) | Chi phí / Tháng (USD) | Cơ sở tính toán chi tiết |
| :---: | :--- | :--- | :---: | :---: | :--- |
| 1 | **Máy chủ Cloud (DO SGP1)** | Droplet 2–4 vCPU, 4–8GB RAM | 609.600 – 1.219.200 VNĐ | $24.00 – $48.00 | Chạy 4 Uvicorn workers, Redis cache, chịu tải burst 15–20 req/s |
| 2 | **Cơ sở dữ liệu (Managed DB)** | DO / Supabase Managed PostgreSQL | 762.000 – 1.524.000 VNĐ | $30.00 – $60.00 | Cụm có PgBouncer connection pool, sao lưu tự động & dự phòng failover |
| 3 | **Cloudflare R2 Storage** | 240.000 ảnh WebP mới/tháng (~12GB) | 50.800 – 76.200 VNĐ | $2.00 – $3.00 | Lưu trữ tích lũy cả năm ($0.015/GB), **$0 băng thông tải ra** |
| 4 | **AI Vision API (4 ảnh/phiếu)** | 240.000 ảnh + 15% chụp lại + chat | 558.800 – 762.000 VNĐ | $22.00 – $30.00 | **4 ảnh/phiếu (1.600 tokens/phiếu)** + lượt chụp lại + chat hỏi đáp |
| 5 | **Giám sát & Sentry (Monitoring)** | Sentry error tracking + Uptime check | 254.000 – 508.000 VNĐ | $10.00 – $20.00 | Giám sát lỗi hệ thống, cảnh báo sự cố 24/7 |
| 6 | **Tên miền & SSL Caddy** | Tên miền quốc tế + SSL Caddy | 25.400 VNĐ | $1.00 | Caddy tự động gia hạn chứng chỉ HTTPS |
| 7 | **Telegram Bot & Lark Base** | Gói chuẩn tích hợp Webhook | 0 VNĐ | $0.00 | Hoàn toàn miễn phí |
| **SUB** | **HẠ TẦNG KỸ THUẬT ĐẦY ĐỦ** | *(Server + DB HA + 4-ảnh AI + R2 + Sentry)* | **2.260.600 – 4.090.800 VNĐ** | **$89.00 – $162.00** | **Khớp chính xác mức ~$90 – $140/tháng đã thảo luận!** |
| **8a** | **Zalo ZNS (Kịch bản Tối ưu Hybrid)** | Gửi ZNS cho ~1.000 tài xế mới + OA | **650.000 – 1.500.000 VNĐ** | **$25.00 – $60.00** | Onboard tài xế mới làm quen, thông báo thường nhật qua Telegram/OA |
| 8b | *Zalo ZNS (Kịch bản Toàn phần 100%)* | *Gửi ZNS cho cả 60.000 phiếu (250đ/tin)* | *15.000.000 VNĐ* | *$590.55* | *Gửi lặp lại cho cả 2 phiếu/ngày của mỗi tài xế* |
| **TỔNG** | **HẠ TẦNG + ZNS HYBRID (KHUYÊN DÙNG)** | **Kỹ thuật đầy đủ + ZNS Hybrid** | **2.910.600 – 5.590.800 VNĐ** | **$114.00 – $222.00** | **~35 – 67 triệu VNĐ / năm** |
| **TỔNG** | *HẠ TẦNG + ZNS TOÀN PHẦN (100% ZNS)* | *Kỹ thuật đầy đủ + 60k tin ZNS* | *17.260.600 – 19.090.800 VNĐ* | *$679.00 – $752.00* | *~207 – 229 triệu VNĐ / năm* |
---

#### Giai đoạn 3: Quy mô 10x (600.000 phiếu/tháng, 1.000–2.000 tài xế)
*Hạ tầng phân tán chịu tải cao — Database High-Availability và đường truyền R2 không giới hạn.*

| STT | Dịch vụ | Quy cách / Gói sử dụng | Chi phí / Tháng (VNĐ) | Chi phí / Tháng (USD) | Ghi chú kỹ thuật |
| :---: | :--- | :--- | :---: | :---: | :--- |
| 1 | **Máy chủ Cloud** | DO SGP1 4 vCPU, 8GB RAM (hoặc 2x Droplet LB) | 1.219.200 VNĐ | $48.00 | Chịu tải mượt mà 20.000 phiếu/ngày |
| 2 | **Cơ sở dữ liệu** | DO Managed PostgreSQL 2 vCPU, 4GB RAM (HA) | 1.524.000 VNĐ | $60.00 | Cụm có node dự phòng Standby failover tự động |
| 3 | **Cloudflare R2** | 30 GB mới/tháng (tích lũy trung bình ~180GB năm 1) | 91.440 VNĐ | $3.60 | $2.70 tiền dung lượng + $0.90 ghi phát sinh |
| 4 | **AI Vision API** | 600.000 lượt gọi (95% DeepSeek + 5% Gemini) | 2.743.200 VNĐ | $108.00 | Trích xuất JSON trực tiếp, không sinh lý luận |
| 5 | **Zalo ZNS** | 600.000 tin nhắn cảm ơn chính thức (250đ/tin) | 150.000.000 VNĐ | $5,905.51 | Kênh chăm sóc tài xế quy mô toàn quốc |
| 6 | **Telegram Bot API** | Telegram Bot API Webhook | 0 VNĐ | $0.00 | Miễn phí 100% |
| 7 | **Tên miền & SSL** | Duy trì tên miền + Caddy HTTPS | 25.400 VNĐ | $1.00 | Đảm bảo kết nối bảo mật Let's Encrypt |
| 8 | **Lark Base** | Workspace Bytedance Lark Cloud | 0 VNĐ | $0.00 | Vận hành nhiều Base phân quyền phòng ban |
| 9 | **Google Drive** | Báo cáo giao nộp khách hàng hàng ngày | 0 VNĐ | $0.00 | Đồng bộ hàng ngàn file WebP hàng ngày |
| **TỔNG** | **Hạ tầng & API Giai đoạn 3** | | **155.603.240 VNĐ** | **$6,126.11** | **1.867.238.880 VNĐ / năm ($73,513.34)** |

---

## 3. Dự Toán Định Biên Nhân Sự & Giờ Làm Việc (Staffing & Operational Labor)

### 3.1 Mô tả phân công trách nhiệm theo 4 vị trí vận hành

1. **Giám sát / Kiểm định chất lượng (QC & Moderation):**
   - **Nhiệm vụ:** Xét duyệt các trường hợp tài xế bấm nút khiếu nại `[ 🙋 Yêu cầu kiểm tra lại ]` (hệ thống giới hạn tối đa 3 lần/8 giờ) và phê duyệt yêu cầu đổi biển số xe `[ 🔄 Cho phép đổi xe ]`.
   - **Công cụ:** Tương tác 1 chạm trực tiếp trên Telegram Admin Group hoặc Kanban trên Lark Base.
   - **Tối ưu tự động hóa:** Pipeline tự động từ chối ảnh mờ (< 25ms), ảnh chụp màn hình thiết bị khác hoặc lệch số học, do đó chỉ có khoảng **0.5% – 1%** tổng sản lượng cần sự can thiệp của con người.
2. **Telesales / Chăm sóc tài xế (Lark Base CRM):**
   - **Nhiệm vụ:** Hướng dẫn tài xế mới gia nhập cách tương tác với Telegram Bot, giải đáp thắc mắc về điểm chụp hợp lệ, theo dõi trạng thái hoạt động của tài xế trên bảng CRM Lark Base.
   - **Công cụ:** Lark Base CRM, điện thoại tổng đài / Zalo kết nối.
3. **Quản lý vận hành & Kế toán chi trả thù lao (Operations & Payroll):**
   - **Nhiệm vụ:** Kiểm soát luồng đối soát doanh thu, kiểm tra bảng tính thù lao tự động (`collaborator_billing_ledger`), xuất file lệnh chi tiền hàng loạt (Batch Payout VietQR / ngân hàng), quản lý ngân sách ZNS và hợp đồng đối tác.
   - **Công cụ:** Lark Base Admin Master Grid, Google Drive Client Delivery, cổng Internet Banking.
4. **Kỹ sư bảo trì hệ thống (System Maintenance Engineer):**
   - **Hình thức:** Gói retainer bán thời gian cho sinh viên năm 3 ngành CNTT/KTPM (mức chi trả: **7 – 12 triệu VNĐ/tháng** theo yêu cầu đề bài).
   - **Nhiệm vụ:** Giám sát Docker compose, Caddy SSL, Redis queue, PostgreSQL backup định kỳ, kiểm tra Circuit Breaker giữa DeepSeek và Gemini, xử lý log lỗi Sentry.

---

### 3.2 Bảng dự toán nhân sự chi tiết theo 3 giai đoạn

#### Giai đoạn 1: Hiện tại (300 phiếu/tháng, 5 tài xế)
*Đội ngũ nòng cốt kiêm nhiệm nhiều vai trò.*

| STT | Vị trí công việc | Số lượng | Hình thức làm việc | Giờ làm / Tháng | Mức lương / Tháng (VNĐ) | Chi phí tương đương (USD) | Chi tiết phân công |
| :---: | :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| 1 | **Giám sát / QC** | 1 | Bán thời gian / Kiêm nhiệm | 15h (~30p/ngày) | 2.000.000 VNĐ | $78.74 | Duyệt vài lượt khiếu nại/đổi xe mỗi tuần |
| 2 | **Telesales / CSKH** | 1 | Bán thời gian / Kiêm nhiệm | 20h (~45p/ngày) | 2.500.000 VNĐ | $98.43 | Đồng hành trực tiếp cùng 5 tài xế ban đầu |
| 3 | **Quản lý & Kế toán** | 1 | Bán thời gian / Kiêm nhiệm | 20h (~45p/ngày) | 3.000.000 VNĐ | $118.11 | Tổng hợp đối soát và thanh toán cuối tháng |
| 4 | **Kỹ sư bảo trì (SV năm 3)** | 1 | Retainer bán thời gian | 50h (~12h/tuần) | 7.000.000 VNĐ | $275.59 | Gói sàn: Triển khai Docker, giám sát hạ tầng |
| **TỔNG** | **Nhân sự Giai đoạn 1** | **4 vị trí** | *(3 kiêm nhiệm + 1 retainer)* | **105h** | **14.500.000 VNĐ** | **$570.87** | **174.000.000 VNĐ / năm ($6,850.39)** |

---

#### Giai đoạn 2: Mục tiêu ban đầu (60.000 phiếu/tháng, ~1.000 tài xế hoạt động)
*Định biên chuyên trách vận hành thương mại quy chuẩn phục vụ đội xe 1.000 tài xế.*

| STT | Vị trí công việc | Số lượng | Hình thức làm việc | Giờ làm / Tháng | Mức lương / Tháng (VNĐ) | Chi phí tương đương (USD) | Chi tiết phân công |
| :---: | :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| 1 | **Giám sát / QC** | 1 | Toàn thời gian (Full-time) | 176h (8h/ngày) | 8.000.000 VNĐ | $314.96 | Duyệt 25–45 khiếu nại & yêu cầu đổi xe/ngày trên Telegram Admin Group |
| 2 | **Telesales / CSKH** | 1 | Toàn thời gian (Full-time) | 176h (8h/ngày) | 8.500.000 VNĐ | $334.65 | Chăm sóc và gọi hướng dẫn tài xế mới trên Lark Base CRM |
| 3 | **Quản lý & Kế toán** | 1 | Toàn thời gian (Full-time) | 176h (8h/ngày) | 10.000.000 VNĐ | $393.70 | Đối soát bảng thù lao tuần, xuất lệnh chuyển khoản ngân hàng |
| 4 | **Kỹ sư bảo trì (SV năm 3)** | 1 | Retainer bán thời gian | 75h (~18h/tuần) | 9.000.000 VNĐ | $354.33 | Gói tiêu chuẩn: Trực uptime, tối ưu SQL B-tree, kiểm tra AI failover |
| **TỔNG** | **Nhân sự Giai đoạn 2** | **4 nhân sự** | *(3 full-time + 1 retainer)* | **603h** | **35.500.000 VNĐ** | **$1,397.64** | **426.000.000 VNĐ / năm ($16,771.65)** |
---

#### Giai đoạn 3: Quy mô 10x (600.000 phiếu/tháng, 1.000–2.000 tài xế)
*Đội ngũ chuyên nghiệp xoay ca 16/24h và hỗ trợ quy mô lớn.*

| STT | Vị trí công việc | Số lượng | Hình thức làm việc | Giờ làm / Tháng | Mức lương / Tháng (VNĐ) | Chi phí tương đương (USD) | Chi tiết phân công |
| :---: | :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| 1 | **Giám sát / QC** | 2 | Toàn thời gian (2 ca sáng/tối) | 352h (2 người x 176h) | 18.000.000 VNĐ | $708.66 | 9.000.000đ/người; xử lý ~150-250 khiếu nại/ngày |
| 2 | **Telesales / CSKH** | 3 | Toàn thời gian (Phụ trách cụm) | 528h (3 người x 176h) | 27.000.000 VNĐ | $1,062.99 | 9.000.000đ/người; quản lý 1.000-2.000 tài xế |
| 3 | **Quản lý Vận hành** | 1 | Toàn thời gian (Lead Vận hành) | 176h | 13.000.000 VNĐ | $511.81 | Điều phối toàn diện và quan hệ đối tác |
| 4 | **Kế toán Chi trả** | 1 | Toàn thời gian (Chuyên trách) | 176h | 10.000.000 VNĐ | $393.70 | Đối soát và kiểm toán thanh toán batch tự động |
| 5 | **Kỹ sư bảo trì (SV năm 3)** | 1 | Retainer bán thời gian | 90h (~22h/tuần) | 12.000.000 VNĐ | $472.44 | Gói trần: On-call 24/7, scale cụm máy chủ, HA |
| **TỔNG** | **Nhân sự Giai đoạn 3** | **8 nhân sự** | *(7 full-time + 1 retainer)* | **1.322h** | **80.000.000 VNĐ** | **$3,149.61** | **960.000.000 VNĐ / năm ($37,795.28)** |

---

## 4. Bảng Tổng Hợp Chi Phí Toàn Diện Theo Tháng & Năm

### 4.1 Bảng đối chiếu tổng chi phí (Hạ tầng + Nhân sự)

| Giai đoạn vận hành | Chi phí Hạ tầng & API / Tháng | Chi phí Nhân sự / Tháng | TỔNG CHI PHÍ / THÁNG (VNĐ) | TỔNG CHI PHÍ / THÁNG (USD) | TỔNG CHI PHÍ / NĂM (VNĐ) | TỔNG CHI PHÍ / NĂM (USD) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Giai đoạn 1** (300 phiếu/tháng) | 406.470 VNĐ | 14.500.000 VNĐ | **14.906.470 VNĐ** | **$586.87** | **178.877.640 VNĐ** | **$7,042.43** |
| **Giai đoạn 2 (Tối ưu ZNS Hybrid)** | **2.910.600 – 5.590.800 VNĐ** | **35.500.000 VNĐ** | **38.410.600 – 41.090.800 VNĐ** | **$1,512.23 – $1,617.75** | **~460.9 – 493.1 triệu VNĐ** | **~$18,146 – $19,413** |
| *Giai đoạn 2 (Toàn phần ZNS 100%)* | *17.260.600 – 19.090.800 VNĐ* | *35.500.000 VNĐ* | *52.760.600 – 54.590.800 VNĐ* | *$2,077.19 – $2,149.24* | *~633.1 – 655.1 triệu VNĐ* | *~$24,926 – $25,791* |
| **Giai đoạn 3 (Tối ưu ZNS Hybrid)** | **8.103.240 VNĐ** | **80.000.000 VNĐ** | **88.103.240 VNĐ** | **$3,468.63** | **1.057.238.880 VNĐ** | **$41,623.58** |
| *Giai đoạn 3 (Toàn phần ZNS 100%)* | *155.603.240 VNĐ* | *80.000.000 VNĐ* | *235.603.240 VNĐ* | *$9,275.72* | *2.827.238.880 VNĐ* | *$111,308.62* |
---

### 4.2 Phân tích hiệu quả kinh tế trên từng phiếu khảo sát (Unit Economics)

Đặc tính mở rộng quy mô phi tuyến tính (economies of scale) thể hiện rõ rệt qua chỉ số chi phí đơn vị:

| Giai đoạn vận hành | Sản lượng khảo sát | Chi phí Kỹ thuật / Phiếu | Chi phí Nhân sự / Phiếu | TỔNG CHI PHÍ / PHIẾU KHẢO SÁT | Tỷ lệ tự động hóa |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Giai đoạn 1 (Pilot)** | 300 phiếu/tháng | 1.354,90 VNĐ ($0.053) | 48.333,33 VNĐ ($1.903) | **49.688,23 VNĐ** ($1.956) | ~80% (tinh chỉnh) |
| **Giai đoạn 2 (Tối ưu Hybrid)** | 60.000 phiếu/tháng | **48,51 VNĐ** ($0.0019) | 591,67 VNĐ ($0.023) | **640,18 VNĐ** ($0.025) | ~92% (thương mại chuẩn) |
| *Giai đoạn 2 (Toàn phần ZNS)* | 60.000 phiếu/tháng | *287,68 VNĐ* ($0.0113) | 591,67 VNĐ ($0.023) | *879,35 VNĐ* ($0.035) | ~92% |
| **Giai đoạn 3 (Tối ưu Hybrid)** | 600.000 phiếu/tháng | **13,51 VNĐ** ($0.0005) | 133,33 VNĐ ($0.005) | **146,84 VNĐ** ($0.006) | > 95% (tối ưu sâu) |
| *Giai đoạn 3 (Toàn phần ZNS)* | 600.000 phiếu/tháng | *259,34 VNĐ* ($0.010) | 133,33 VNĐ ($0.005) | *392,67 VNĐ* ($0.015) | > 95% |
1. **Zalo ZNS chiếm tỷ trọng lớn trong chi phí API ở Giai đoạn 2 & 3:** Với 250 VNĐ/tin, ZNS chiếm hơn **90% - 96%** tổng chi phí hạ tầng & API (15.000.000 VNĐ ở G2 và 150.000.000 VNĐ ở G3). Nếu doanh nghiệp đã có tài khoản Zalo OA xác thực tích hợp template CSKH miễn phí hoặc sử dụng kênh thông báo Telegram trực tiếp cho tài xế làm kênh chính, chi phí hạ tầng kỹ thuật thực tế chỉ còn **~1.557.020 VNĐ/tháng ($61.30)** ở Giai đoạn 2 và **~5.603.240 VNĐ/tháng ($220.60)** ở Giai đoạn 3.
2. **Chi phí AI Vision cực kỳ rẻ:** Nhờ áp dụng chiến thuật tiền xử lý ảnh WebP (~49KB) và mô hình **DeepSeek Flash / Gemini 3.5 Flash no-reasoning** chỉ trích xuất dữ liệu thô, chi phí AI trên 600.000 phiếu chỉ mất **~2.743.200 VNĐ/tháng (~$108/tháng)** — tương đương chỉ **~4.57 VNĐ / phiếu**.
3. **Chi phí nhân sự giảm mạnh trên mỗi đơn vị phiếu:** Từ mức ~48.333 VNĐ/phiếu ở pilot giảm mạnh xuống chỉ còn **~392 VNĐ/phiếu** ở Giai đoạn 3 nhờ cơ chế tự động hóa loại trừ gian lận, giới hạn khiếu nại (3 lần/8h) và phê duyệt 1-click trên Telegram.
