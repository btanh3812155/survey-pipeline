# Hướng dẫn sử dụng dành cho Cộng tác viên (CTV) - Trust Bridge

Tài liệu này hướng dẫn đầy đủ quy trình làm việc của **Cộng tác viên (CTV)** trên hệ thống Trust Bridge: kích hoạt tài khoản Telegram, đăng nhập cổng `/ctv`, hướng dẫn tài xế làm khảo sát, theo dõi mục tiêu, xử lý phiếu bị từ chối và liên hệ hỗ trợ.

> **Lưu ý về vai trò**: Tài xế là người tham gia khảo sát do CTV trực tiếp hướng dẫn tại hiện trường; CTV là người chịu trách nhiệm thực hiện quy trình nộp phiếu, theo dõi kết quả duyệt và nhận thù lao theo phiếu hợp lệ (`APPROVED`).

---

## 1. Kích hoạt tài khoản Telegram CTV (Lần đầu)

Trước khi đăng nhập website hoặc nhận thông báo kết quả duyệt phiếu, CTV phải liên kết số điện thoại của mình với bot Telegram CTV.

### Các bước thực hiện
1. Nhận **liên kết mời riêng** do quản trị viên gửi trực tiếp (dạng `https://t.me/<ten_bot>?start=ctv_<token>`).
   - Không chia sẻ công khai hoặc chuyển tiếp liên kết mời này cho người khác.
2. Mở liên kết trên ứng dụng Telegram và bấm **Start**.
3. Trong cuộc trò chuyện **riêng tư (1-1)** với bot, bấm nút **Xác Thực Số Điện Thoại** để chia sẻ **danh bạ của chính tài khoản Telegram đó**.
4. Chờ bot xác nhận liên kết thành công với mã CTV của bạn.

### Các trường hợp bị từ chối khi xác thực
- Chỉ bấm **Start** mà chưa chia sẻ danh bạ.
- Gõ số điện thoại bằng tin nhắn chữ thay vì dùng nút chia sẻ danh bạ.
- Chuyển tiếp/gửi danh bạ của người khác.
- Thực hiện thao tác trong nhóm chat thay vì chat riêng với bot.
- Số điện thoại đã thuộc về một CTV khác hoặc đang có tranh chấp liên kết.
- Mã CTV đang bị tắt (`inactive`) hoặc đã bị xóa.

---

## 2. Đổi tài khoản Telegram hoặc đổi số điện thoại

### Trường hợp A: Đổi sang tài khoản Telegram mới nhưng giữ nguyên số điện thoại đã đăng ký
Bạn **tự thực hiện được** mà không cần truy cập tài khoản Telegram cũ và không bắt buộc xin lại link mời:
1. Đăng nhập tài khoản Telegram mới (đang gắn **đúng số điện thoại đã đăng ký** với mã CTV của bạn).
2. Mở chat riêng với bot Telegram CTV.
3. Bấm **Xác Thực Số Điện Thoại** và chia sẻ danh bạ của chính bạn.
4. Hệ thống sẽ tự động chuyển quyền CTV sang tài khoản Telegram mới.

**Hệ quả sau khi đổi tài khoản Telegram:**
- Tài khoản Telegram cũ mất quyền CTV; toàn bộ phiên đăng nhập trình duyệt và mã OTP cũ lập tức hết hiệu lực.
- Các thông báo kết quả khảo sát mới sẽ gửi về tài khoản mới; thông báo cũ đang chờ gửi tới tài khoản cũ bị chặn.
- Mã CTV, số điện thoại, lịch sử bài nộp, mục tiêu và các khoản đã quyết toán vẫn giữ nguyên cho mã CTV của bạn.
- Các cuộc trò chuyện hỗ trợ cũ gắn với tài khoản Telegram cũ sẽ không tự chuyển sang tài khoản mới.

### Trường hợp B: Muốn đổi số điện thoại đăng ký
Bot Telegram **không** cho phép tự đổi số điện thoại đã đăng ký. Bạn phải liên hệ quản trị viên để cập nhật số điện thoại trong hệ thống; sau khi quản trị viên cập nhật, các phiên đăng nhập cũ sẽ hết hiệu lực và bạn cần xác thực lại bằng số mới.

---

## 3. Đăng nhập Cổng Cộng tác viên (`/ctv`)

1. Truy cập miền khảo sát (ví dụ: `https://survey.trustbridgevn.com`). Khi chưa đăng nhập, hệ thống tự chuyển đến trang `/ctv/login`.
2. Chọn **Mã CTV** trong danh sách **hoặc** nhập **Số điện thoại đã xác minh**.
   - Lưu ý: Cả hai cách đều bắt buộc nhập mã OTP gửi qua Telegram; không thể đăng nhập chỉ bằng mã CTV.
3. Mở bot Telegram CTV để lấy **mã OTP gồm 6 chữ số**.
4. Nhập mã OTP trên **chính trình duyệt** vừa yêu cầu:
   - Mã OTP có hiệu lực trong **5 phút**.
   - Chỉ dùng **1 lần** và cho phép thử sai tối đa **5 lần**.
5. Sau khi đăng nhập thành công, phiên làm việc được giữ trong **30 ngày** trên thiết bị đó.
   - Bấm **Đăng xuất** khi dùng chung thiết bị.

### Nội dung trên Cổng CTV (`/ctv`)
- **Thông tin của bạn**: Mã CTV, họ tên, số điện thoại và trạng thái xác minh Telegram.
- **Mục tiêu và tiến độ**:
  - Chỉ tính các phiếu đã được duyệt (**`APPROVED`**). Các phiếu `MANUAL_REVIEW`, `APPEAL_PENDING` hoặc `REJECTED` không tính vào tiến độ.
  - Mục tiêu dùng để **theo dõi tiến độ**, **không chặn nộp phiếu** khi bạn đã đạt hoặc vượt mục tiêu.
  - Có thể hiển thị song song mục tiêu theo kỳ cuốn chiếu (ví dụ 7 ngày gần nhất) và mục tiêu toàn thời gian.
- **Khảo sát đang mở**:
  - Chỉ hiển thị những khảo sát công khai đang mở mà **bạn được phân công**.
  - Bấm **Mở khảo sát** để mở đúng liên kết đã gắn sẵn mã CTV của bạn (`?ref=<MA_CTV>`).
  - Khảo sát riêng tư (`private`), đã đóng hoặc đã đạt giới hạn phản hồi sẽ không hiển thị.

---

## 4. Hướng dẫn tài xế thực hiện khảo sát

### Chuẩn bị trước khi mở biểu mẫu
- Đảm bảo mở đúng liên kết khảo sát của chính bạn (từ cổng `/ctv`, từ lệnh `/forms` trên bot Telegram hoặc mã QR do quản trị viên cấp).
- **Bắt buộc bật định vị GPS** trên điện thoại và cho phép trình duyệt truy cập vị trí.
  - Nếu tắt GPS hoặc trình duyệt không lấy được tọa độ hợp lệ, hệ thống sẽ **chặn gửi bài** (không thể gửi khảo sát không có GPS).
- Sử dụng **camera trực tiếp trên trình duyệt (WebRTC)** để chụp rõ nét các màn hình/giấy tờ theo đúng từng ô ảnh yêu cầu; không dùng ảnh chụp lại màn hình mờ, ảnh cắt ghép hoặc ảnh đã dùng cho phiếu khác.

### Quy trình nộp phiếu
1. Mở liên kết khảo sát -> kiểm tra mã CTV đã được gắn sẵn.
2. Cho tài xế đọc mục **Đồng thuận xử lý dữ liệu (Nghị định 13/2023/NĐ-CP)** và ký xác nhận nếu đồng ý.
   - Nếu tài xế chọn **"Tôi không đồng ý"**: hệ thống ẩn các câu hỏi dữ liệu cá nhân, không lưu số điện thoại/tên tài xế, không gửi thông báo và phiếu đó **không được duyệt (`APPROVED`) / không tính thù lao hoặc tiến độ CTV**.
3. Điền đầy đủ câu hỏi bắt buộc (lưu ý định dạng số điện thoại `0` + 9 số hoặc `+84` + 9 số và các khoảng số hợp lệ của từng chiến dịch).
4. Chụp ảnh từng slot theo hướng dẫn. Hệ thống chạy **kiểm tra nhanh ngay khi chụp (pre-check)** để cảnh báo sớm nếu ảnh bị mờ hoặc sai loại ảnh.
5. Bấm **Gửi khảo sát** và chờ hệ thống trả về **Mã phiếu** (ví dụ `VN-...`) cùng trạng thái ban đầu:
   - **`MANUAL_REVIEW` (Chờ đối soát thủ công)**: Mặc định đa số chiến dịch cần quản trị viên kiểm tra và duyệt qua Telegram trước khi tính hợp lệ.
   - **`APPROVED` (Đã duyệt)**: Xuất hiện ngay nếu chiến dịch tắt chế độ bắt buộc duyệt thủ công và mọi kiểm tra tự động đều đạt, hoặc sau khi quản trị viên bấm duyệt.
   - **`REJECTED` (Bị từ chối)**: Kèm lý do cụ thể (ảnh mờ, thiếu thông tin, trùng ảnh, không đạt quy tắc chiến dịch...).

### Quy định về tài xế nộp nhiều lần
- **Một tài xế có thể tham gia nhiều chiến dịch (biểu mẫu) khác nhau**.
- Nếu một phiếu bị **`REJECTED`**, tài xế **được phép làm lại** khảo sát đó (dùng liên kết làm lại hoặc chụp bổ sung/phúc khảo).

### Các thông báo chặn truy cập thường gặp khi mở hoặc gửi form
- **"Không có quyền truy cập khảo sát / CTV chưa được phân công" (HTTP 403)**: Bạn chưa được phân công khảo sát này, hoặc quản trị viên vừa thu hồi phân công (kể cả khi đang làm dở giữa chừng, hệ thống kiểm tra quyền ngay tại thời điểm bấm gửi). Liên hệ quản trị viên nếu đây là nhầm lẫn.
- **"Khảo sát đã đủ số lượng phản hồi / đạt giới hạn" (HTTP 409)**: Chiến dịch đã đạt đủ số lượng phiếu `APPROVED` tối đa. Không tiếp tục thu thập thêm cho biểu mẫu này.
- **"Bắt buộc bật định vị GPS để gửi khảo sát" (HTTP 400)**: Bật GPS/dịch vụ vị trí trên điện thoại, cấp quyền vị trí cho trình duyệt, tải lại tín hiệu GPS rồi gửi lại.
- **"Biểu mẫu riêng tư" (HTTP 403)**: Biểu mẫu đang ở chế độ riêng tư (chỉ nhân sự nội bộ được mở).

---

## 5. Xử lý khi phiếu bị từ chối (`REJECTED`)

Khi phiếu bị từ chối, CTV có **3 cách xử lý** tùy tình huống:

### Cách 1: Tự làm lại phiếu mới không cần hỗ trợ (Retry Link)
Dùng khi lỗi rõ ràng do ảnh mờ, chụp thiếu hoặc nhập sai thông tin và tài xế vẫn đang ở cạnh bạn:
- Bấm **liên kết làm lại** (retry link) kèm trong thông báo từ chối hoặc mở lại khảo sát từ cổng `/ctv`.
- Liên kết làm lại vẫn giữ đúng mã CTV của bạn để nộp một phiếu mới sạch.

### Cách 2: Gửi phúc khảo ngay trên trang web (Web Appeal)
Dùng khi bạn tin rằng hệ thống từ chối nhầm và muốn người thật kiểm tra lại phiếu hiện tại:
1. Bấm nút **Yêu cầu kiểm tra lại / Phúc khảo** ngay trên màn hình kết quả khảo sát (nhập lý do ngắn gọn).
2. Phiếu chuyển sang trạng thái **`APPEAL_PENDING`** và xuất hiện trên hàng đợi phúc khảo của ban quản trị (web `/admin/appeals` và Telegram).
3. Nếu gửi nhầm, bạn có thể bấm **Hủy yêu cầu kiểm tra lại**.
4. Giới hạn: tối đa **20 lượt phúc khảo web / giờ** cho mỗi số điện thoại.

### Cách 3: Phúc khảo hoặc chụp bổ sung ảnh qua Bot Telegram
Dùng sau khi đã rời màn hình web hoặc khi cần bổ sung ảnh cho đúng phiếu đã nộp:
1. Mở tin nhắn kết quả trong bot Telegram CTV -> bấm nút **Phúc khảo `#<MA_PHIEU>`** (hoặc gửi lệnh `/appeal <MA_PHIEU>`).
2. Nếu phiếu thiếu/mờ ảnh, gửi ảnh bổ sung trực tiếp trong cuộc trò chuyện theo hướng dẫn của bot để hệ thống kiểm tra lại.
3. Khi ban quản trị duyệt hoặc từ chối phúc khảo (dù thao tác trên web hay Telegram), bot sẽ gửi kết quả cuối cùng về cho bạn.

---

## 6. Các lệnh hữu ích trên Bot Telegram CTV

| Lệnh / Nút | Chức năng |
|---|---|
| `/forms` | Xem danh sách các khảo sát đang mở **được phân công cho bạn** kèm sẵn liên kết `?ref=<MA_CTV>` |
| `/quota` (hoặc `/hanmuc`) | Xem mục tiêu và số phiếu `APPROVED` hiện tại của bạn |
| `📋 Khảo Sát Của Tôi` / `/history` | Tra cứu lịch sử các phiếu gần đây của bạn |
| `/appeal <MA_PHIEU>` | Gửi yêu cầu phúc khảo cho phiếu bị từ chối |
| Nhắn tin trực tiếp cho bot | Mở cuộc trò chuyện hỗ trợ trực tiếp với ban quản trị (hỗ trợ CTV chỉ thực hiện qua Telegram) |

---

## 7. Xử lý sự cố nhanh cho CTV

| Vấn đề | Nguyên nhân & Cách xử lý |
|---|---|
| Không nhận được mã OTP đăng nhập `/ctv` | Kiểm tra đã hoàn tất bước **Xác Thực Số Điện Thoại** trong chat riêng với bot và chưa chặn (block) bot. Thử lại sau khi mở khóa bot. |
| Đang đăng nhập `/ctv` tự dưng bị đăng xuất | Quản trị viên vừa sửa số điện thoại/trạng thái CTV, hoặc bạn vừa đổi sang tài khoản Telegram mới. Đăng nhập lại bằng OTP trên tài khoản Telegram hiện tại. |
| Không thấy khảo sát trên `/ctv` hoặc `/forms` | Khảo sát chưa được phân công cho bạn, đang ở chế độ riêng tư, hoặc đã đủ giới hạn số phiếu `APPROVED`. Liên hệ quản trị viên để kiểm tra phân công. |
| Không gửi được bài do lỗi GPS | Bật Vị trí (Location/GPS) của máy, vào cài đặt trình duyệt cấp quyền Vị trí cho trang khảo sát, di chuyển ra nơi thoáng sóng nếu ở trong nhà rồi bấm gửi lại. |
| Số phiếu trên `/ctv` thấp hơn số phiếu đã nộp | Tiến độ mục tiêu chỉ đếm phiếu **`APPROVED`**. Phiếu đang `MANUAL_REVIEW`, `APPEAL_PENDING` hoặc `REJECTED` chưa được cộng. |
| Gửi phiếu gặp mạng chập chờn, sợ bị trùng | Hệ thống dùng khóa chống trùng (idempotency). Giữ nguyên màn hình và thử gửi lại; không mở tab mới gửi trùng cùng một bộ ảnh. |
