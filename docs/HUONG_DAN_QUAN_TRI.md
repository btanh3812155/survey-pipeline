# Hướng dẫn quản trị Trust Bridge

Tài liệu dành cho quản trị viên vận hành hệ thống Trust Bridge: quản lý biểu mẫu, module kiểm tra, xuất Google Drive và tài khoản quản trị.

Ảnh chụp trong tài liệu lấy từ môi trường staging. Tên biểu mẫu, thư mục và tài khoản trong ảnh chỉ là dữ liệu minh họa.

## 1. Truy cập và đăng nhập

Mở miền quản trị đã được cấp, ví dụ:

```text
https://admin.trustbridgevn.com
```

Hệ thống dùng trang đăng nhập riêng, không còn hộp thoại Basic Auth của trình duyệt.

![Đăng nhập quản trị](images/admin-tutorial/login.png)

Nhập:

- Tên đăng nhập: tài khoản quản trị đã được cấp
- Mật khẩu: mật khẩu của tài khoản đó

Phiên đăng nhập được giữ bằng cookie `HttpOnly` trong 30 ngày, kể cả khi đóng và mở lại trình duyệt. Tài khoản được xác thực từ cơ sở dữ liệu, không dùng mật khẩu môi trường. Tài khoản bị tắt, bị xóa, đổi mật khẩu hoặc không tồn tại sẽ không đăng nhập được. Hãy đăng xuất trên thiết bị dùng chung.

Bấm **Đăng xuất** trên thanh điều hướng để kết thúc phiên.

### Vai trò

| Vai trò | Quyền chính |
|---|---|
| `superadmin` (**Quản trị**) | Toàn bộ quản trị, gồm quản lý tài khoản, phân quyền, thùng rác, email khôi phục |
| `manager` | Biểu mẫu, dữ liệu nộp, audit, media, CTV, ảnh/object, module, exports |
| `reviewer` | Dữ liệu nộp và các màn hình được phép xem |
| `viewer` | Chỉ xem các màn hình được phép |

Thanh điều hướng có nút `VI/EN` để đổi ngôn ngữ giao diện. Ngôn ngữ mặc định là tiếng Việt và được ghi nhớ trên trình duyệt.

## 2. Tổng quan menu

Menu trên cùng bên trái gồm:

- **Biểu mẫu**: danh sách và trình chỉnh sửa biểu mẫu
- **Dữ liệu nộp**: xem, lọc và xuất dữ liệu bài nộp
- **Audit**: nhật ký kiểm toán
- **Media**: quản lý ảnh và tệp tĩnh
- **CTV**: danh bạ cộng tác viên, thêm/sửa/xóa, bài nộp, mục tiêu và tạo liên kết/QR theo biểu mẫu.
- **Ảnh/Object**: định nghĩa object ảnh và engine đang có
- **Module**: logic, object và engine module trong cơ sở dữ liệu
- **Exports**: cấu hình xuất Google Drive
- **Tài khoản của tôi**: xem vai trò, trạng thái, email khôi phục (superadmin) và đổi mật khẩu
- **Quản lý tài khoản**: chỉ superadmin
- **Thùng rác**: khôi phục biểu mẫu và bài nộp đã xóa
- **Email khôi phục**: chỉ superadmin

## 3. Quản lý biểu mẫu

### 3.1 Danh sách biểu mẫu

Mở **Biểu mẫu**.

![Danh sách biểu mẫu](images/admin-tutorial/forms-list.png)

Các thao tác chính:

- **Tạo Biểu Mẫu Mới**: tạo biểu mẫu mới.
- **Nhập biểu mẫu**: nhập một form hoặc một gói nhiều form.
- **Xuất tất cả**: tải toàn bộ biểu mẫu dưới dạng JSON.
- Ô tìm kiếm: lọc theo tên hoặc mã form.
- Bộ lọc trạng thái và kiểu sắp xếp.
- Nút trên từng thẻ:
  - QR: mở mã QR và liên kết chia sẻ
  - Mở: mở form công khai
  - Bút chì: mở trình chỉnh sửa
  - Tải xuống: xuất riêng biểu mẫu đó

### 3.2 Trình chỉnh sửa biểu mẫu

Mở biểu mẫu cần sửa bằng nút bút chì.

![Trình chỉnh sửa biểu mẫu](images/admin-tutorial/form-editor.png)

Trình chỉnh sửa có ba tab:

1. **Thông tin chung**: mã form, tiêu đề, mô tả, giới hạn phản hồi, chế độ test, CTV, thông báo Telegram.
2. **Tùy chọn & Cài đặt**: kiểm tra ảnh toàn form và danh sách module.
3. **Danh sách câu hỏi**: thêm, sửa, sắp xếp câu hỏi, điều kiện hiển thị, chữ ký, consent gate và slot ảnh.

Bấm **Lưu & Kích hoạt biểu mẫu** sau khi sửa. Nếu form đã tồn tại, thao tác lưu sẽ cập nhật file cấu hình và registry.

### 3.3 Module kiểm tra của biểu mẫu

Chuyển sang tab **Tùy chọn & Cài đặt**, kéo xuống mục **Modules**.

![Module kiểm tra của biểu mẫu](images/admin-tutorial/form-modules.png)

Cách thêm một kiểm tra:

1. Bấm **+ Thêm Module**.
2. Đặt tên module.
3. Chọn **Loại module**:
   - `Module logic`: chạy implementation logic đã đăng ký
   - `Engine kiểm tra`: chạy engine kiểm tra
   - `Tổng hợp (tất cả)`: gộp nhiều module phía trên
   - `Phân bố (thống kê đáp án)`: thống kê đáp án trên toàn bộ bài nộp
4. Chọn engine hoặc module tương ứng.
5. Chọn nguồn đầu vào:
   - Câu hỏi
   - Đầu ra của module trước
   - Toàn bộ module trước
   - Giá trị cố định
6. Bật **Từ chối bài gửi khi module không đạt** nếu đây là kiểm tra chặn.
7. Lưu biểu mẫu.

Các kiểm tra mặc định hiện dùng module, không còn `ValidationRule` trên từng câu hỏi:

- `phone_format`: engine `regex`, trường `driver_phone`, chấp nhận `0` + 9 chữ số hoặc `+84` + 9 chữ số.
- `daily_trips_range`: engine `number_range`, trường `cau_12`, giới hạn 1 đến 200.

Nếu module không đạt và có `reject_on_false`, bài nộp bị từ chối với `reject_message`. Nếu module lỗi cấu hình, hệ thống ghi nhận lỗi module thay vì bỏ qua âm thầm.

## 4. Module và kiểm tra ảnh

### 4.1 Trang Module

Mở **Module**.

![Danh sách module](images/admin-tutorial/modules.png)

Trang này là nguồn cấu hình chính cho:

- **Module object**: định nghĩa object ảnh, prompt, trường cần trích xuất, engine binding.
- **Module logic**: implementation logic an toàn đã biên dịch trong ứng dụng.
- **Module engine**: các engine kiểm tra như `regex`, `number_range`, `required_fields`, `date_range`, `arithmetic`.

Có thể tìm kiếm, mở rộng từng module, bật/tắt, sửa hoặc xóa. Việc xóa engine đang được object đang bật sử dụng sẽ bị chặn.

### 4.2 Trang Ảnh/Object

Mở **Ảnh/Object**.

![Object ảnh và engine](images/admin-tutorial/image-objects.png)

Trang này dùng để:

- Tạo object ảnh mới.
- Xem và sửa prompt, extractor, classify role, trường dự kiến, engine binding của object.
- Xem danh sách engine hiện có.

Lưu ý phân biệt:

- **Object** là định nghĩa cách đọc một loại ảnh.
- **Engine** là logic kiểm tra dùng chung.
- Object chỉ **tham chiếu** engine bằng ID; định nghĩa engine nằm trong trang **Module**.
- Danh sách engine trong trang Ảnh/Object là danh mục chỉ đọc để biết object có thể gắn engine nào. Muốn sửa engine, vào **Module**.

## 5. Xuất Google Drive

Mở **Exports**.

### 5.1 Drive của tôi

![Drive của tôi](images/admin-tutorial/exports-root.png)

Màn hình gốc có hai thư mục:

- **clients**: cấu hình xuất cho khách hàng
- **internal**: cấu hình xuất nội bộ

Panel bên phải dùng để:

- Đổi tên thư mục Drive.
- Chạy xuất thủ công bằng **Chạy xuất**.
- Xem báo cáo xuất gần nhất.

### 5.2 Sửa khách hàng

Mở thư mục **clients**.

![Sửa khách hàng](images/admin-tutorial/exports-clients.png)

Tại đây:

- Danh sách folder khách hàng hiện có.
- Panel **Sửa khách hàng** để tạo cấu hình mới.
- Bấm vào một folder khách hàng để mở cấu hình của khách đó.

### 5.3 Quản lý form của khách hàng

Trong folder khách hàng:

- Các form khách hàng nhận được hiển thị thành các file card.
- Panel bên phải **Quản lý form của khách hàng này** dùng để:
  - chọn form khách hàng nhận
  - chỉnh trường mặc định
  - trạng thái
  - loại xe
  - khoảng ngày
  - ảnh đính kèm
  - xóa cấu hình khách hàng

Bấm vào một form card để mở cấu hình form đó.

### 5.4 Quyền truy cập của khách hàng trong form

Trong folder form:

- Chọn **Trường dữ liệu** được xuất.
- Chọn **Trạng thái** được xuất.
- Chọn **Loại xe**.
- Chọn chế độ ngày:
  - Kế thừa khoảng của khách hàng
  - Không giới hạn
  - Ngày tùy chỉnh
- Bật/tắt ảnh bài nộp.
- Bấm **Lưu đầu ra form**.
- Nếu form có override, có thể **Đặt lại mặc định khách hàng**.

### 5.5 Chạy xuất

Từ màn hình gốc của Exports, bấm **Chạy xuất**. Yêu cầu trả về ngay và tiến trình chạy nền. Thẻ **Tiến trình xuất Drive** hiển thị trạng thái, số khách hàng, form, dòng, sheet và ảnh đang xử lý; trang tự làm mới khi xong. Hệ thống dùng khóa tư vấn trong cơ sở dữ liệu để tránh hai lần xuất chạy chồng nhau. Xem kết quả cuối ở **Báo cáo xuất gần nhất**.

### 5.6 Liên kết Telegram cho cộng tác viên

1. Vào **Exports** và bấm **Chạy xuất**. Khi xuất hoàn tất, mở bảng **CTV_Info** trong thư mục **internal** trên Google Drive.
2. Cột thứ ba, **Telegram Bot**, chứa liên kết mời riêng của từng mã CTV. Quản trị viên gửi đúng liên kết này cho CTV qua tin nhắn riêng.
3. CTV mở liên kết, bấm **Start**, rồi bấm **📱 Xác Thực Số Điện Thoại** trong cuộc trò chuyện riêng với bot để chia sẻ **danh bạ của chính mình**.
4. Chỉ sau khi xác thực danh bạ thành công, hệ thống mới liên kết tài khoản Telegram vào đúng mã CTV. Nếu danh bạ CTV chưa có số đăng ký, lần liên kết đầu tiên lưu số đã xác thực; nếu đã có số, phải xác thực đúng số đó. Chỉ bấm **Start**, nhập số bằng chữ, gửi danh bạ của người khác hoặc thao tác trong nhóm không cấp quyền CTV.

**Đổi tài khoản Telegram, giữ nguyên số:** CTV mở bot bằng tài khoản mới và bấm **📱 Xác Thực Số Điện Thoại**, chia sẻ danh bạ của chính tài khoản mới với **đúng số đã đăng ký**. Không cần truy cập tài khoản Telegram cũ và không bắt buộc dùng lại link mời. Hệ thống chỉ chấp nhận một CTV đang hoạt động, không trùng số với CTV khác (kể cả bản ghi bị tắt), không có tranh chấp liên kết số. Link mời vẫn có thể dùng để bắt đầu nhưng không thay thế bằng chứng số điện thoại.

Sau khi đổi thành công, tài khoản cũ mất quyền CTV; phiên trình duyệt và OTP cũ hết hiệu lực. Thông báo CTV mới gửi đến tài khoản mới; thông báo đang chờ gửi đến tài khoản cũ bị chặn, không gửi lại lịch sử tài xế trước thời điểm xác minh mới. Mã CTV, số đăng ký, bài nộp, quota, chủ sở hữu khoản thanh toán và các khoản đã quyết toán không bị chuyển đổi. Khoản còn chờ của tài khoản cũ chỉ giữ điều kiện thanh toán qua chuỗi đổi Telegram cùng CTV đã được audit, khi CTV và tài khoản nhận hiện tại vẫn hoạt động; vô hiệu hóa hoặc sửa danh tính qua quản trị làm chuỗi này mất hiệu lực. Hội thoại hỗ trợ thông thường gắn với tài khoản Telegram đã mở, không tự chuyển sang tài khoản mới.

**Đổi số điện thoại:** CTV phải liên hệ quản trị viên sửa danh bạ trên website; bot không thay số đã đăng ký. PostgreSQL là nguồn dữ liệu chính; **CTV_Info** trên Google Sheets là đầu ra xuất từ PostgreSQL, không phải nơi sửa danh tính. Nhật ký **Audit** có sự kiện `CTV_TELEGRAM_REPLACED` cho lần đổi thành công; lỗi audit làm toàn bộ lần đổi bị hủy.

**Bảo mật:** Liên kết chứa mã mời bí mật; không đăng công khai hoặc chuyển cho người không phụ trách mã CTV đó. Liên kết cũ chỉ chứa mã CTV (dạng `?start=ctv_<mã_ctv>`) không còn hợp lệ. CTV bị vô hiệu hóa hoặc đã xóa sẽ bị từ chối, kể cả khi đã bấm **Start** trước đó.

### 5.7 Cổng cộng tác viên và OTP đăng nhập

1. Mở miền khảo sát, ví dụ `https://survey.trustbridgevn.com`. Khi chưa đăng nhập, hệ thống mở trang đăng nhập CTV.
2. Chọn mã CTV trong danh sách hoặc nhập số điện thoại đã xác minh. **Cả hai cách đều cần OTP**, không thể đăng nhập chỉ bằng mã CTV công khai.
3. Bot Telegram dành cho CTV gửi mã gồm 6 chữ số vào tài khoản đã liên kết. Nhập mã trên chính trình duyệt đã yêu cầu; mã hết hạn sau 5 phút, dùng một lần và có tối đa 5 lần thử. Nếu chưa nhận được mã, kiểm tra đã hoàn tất bước xác minh danh bạ ở mục 5.6 và chưa chặn bot.
4. Sau khi đăng nhập, CTV xem thông tin cá nhân, mục tiêu/quota cùng tiến độ và các khảo sát công khai đang mở. Bấm **Mở khảo sát** để mở liên kết trên cùng miền. Biểu mẫu bật theo dõi CTV có sẵn mã CTV; biểu mẫu tắt theo dõi được ghi chú rõ và không tính vào tiến độ CTV.

Mục tiêu là chỉ tiêu theo dõi, không chặn nộp phiếu khi đã đạt. Mục tiêu cuốn chiếu và toàn thời gian được hiển thị riêng. Khảo sát riêng tư, lưu trữ hoặc đã đủ giới hạn phản hồi không xuất hiện trong danh sách.

Phiên CTV được giữ trong 30 ngày trên thiết bị này. Bấm **Đăng xuất** để kết thúc; thao tác này không đăng xuất tài khoản quản trị riêng. Tắt/xóa CTV hoặc thay đổi mã/số điện thoại/Telegram làm phiên và OTP cũ không còn hợp lệ, kể cả khi bật lại hoặc khôi phục thông tin cũ. Sửa tên hoặc ghi chú không làm mất phiên. Số chưa xác minh, số bị tranh chấp hoặc số không thuộc CTV không được đăng nhập. Lỗi cơ sở dữ liệu, Redis hay Telegram không được thay thế bằng số liệu giả hoặc bỏ qua OTP.

### 5.8 Quản lý CTV và tạo liên kết/QR

- Trên máy tính, nút sửa (biểu tượng bút) và xóa (biểu tượng thùng rác) nằm ngang. Không còn nút tắt nhanh hay cột **Thông tin** riêng; tên và ghi chú vẫn có trong phần chi tiết/chỉnh sửa.
- Trên điện thoại, mỗi dòng chỉ hiện mã CTV và tam giác xổ xuống. Chạm mã để mở toàn bộ thông tin, số điện thoại, Telegram, trạng thái, bài nộp, mục tiêu và các nút thao tác.
- Trong phần tạo liên kết/QR, chọn CTV và biểu mẫu rồi bấm **Tạo**. Chỉ CTV đang hoạt động và biểu mẫu công khai, đang mở, có theo dõi CTV được dùng để tạo liên kết có ghi nhận mã.
- Liên kết có nút **Sao chép** và **Chia sẻ**. QR có **Sao chép ảnh**, **Chia sẻ ảnh** và **Tải PNG**. QR và liên kết cùng trỏ đến biểu mẫu/mã CTV đã chọn trên chính miền hiện tại. Đổi lựa chọn sẽ xóa kết quả cũ để tránh chia sẻ nhầm.
- Sao chép ảnh và chia sẻ tùy khả năng/quyền của trình duyệt, thường cần HTTPS. Nếu không được hỗ trợ, dùng tải PNG hoặc sao chép liên kết; thông báo lỗi không có nghĩa là đã sao chép/chia sẻ thành công.


## 6. Tài khoản và phân quyền

### 6.1 Tài khoản của tôi

Mở **Tài khoản của tôi**.

![Tài khoản của tôi](images/admin-tutorial/my-account.png)

Trang này hiển thị:

- Tên đăng nhập
- Vai trò hiện tại
- Trạng thái tài khoản
- Thời điểm cập nhật
- Email khôi phục, chỉ với superadmin
- Form đổi mật khẩu

Superadmin thấy thêm khối **Email khôi phục** và nút **Đổi email khôi phục**. Việc đổi email đi qua trang xác nhận riêng, cần truy cập cả hộp thư hiện tại và hộp thư mới. Các vai trò khác không thấy email này trong HTML.

Đổi mật khẩu cần nhập mật khẩu hiện tại, mật khẩu mới và xác nhận. Mật khẩu mới phải dài 12 đến 1024 ký tự ASCII in được.

### 6.2 Quản lý tài khoản

Chỉ superadmin thấy mục **Quản lý tài khoản**. Trang này có:

- **Tạo tài khoản quản trị**
- **Phân quyền tài khoản**: chọn tài khoản và vai trò mới
- Bảng tài khoản với nút **Quản lý** cho từng tài khoản
- **Thùng rác tài khoản**: khôi phục trong vòng 30 ngày

Quy tắc an toàn:

- Không thể tự tắt, tự xóa hoặc tự hạ vai trò của mình.
- Phải giữ ít nhất một superadmin đang hoạt động.
- Tài khoản đã xóa không đăng nhập được.
- Mật khẩu của tài khoản khác không được đặt hộ; mỗi người tự đổi qua **Tài khoản của tôi**.

## 7. Các màn hình vận hành khác

- **Dữ liệu nộp**: xem và lọc bài nộp, xuất CSV/JSON. Không chia sẻ ảnh chụp màn hình có số điện thoại hoặc thông tin tài xế.
- **Audit**: nhật ký kiểm toán, phục vụ truy vết thay đổi.
- **Media**: quản lý tệp tĩnh và ảnh minh họa.
- **CTV**: danh bạ cộng tác viên, thêm/sửa/xóa, bài nộp, mục tiêu và tạo liên kết/QR theo biểu mẫu.
- **Thùng rác**: khôi phục biểu mẫu hoặc bài nộp đã xóa. Không xóa vĩnh viễn nếu chưa có phê duyệt.

## 8. Checklist nhanh sau khi cấu hình

- [ ] Biểu mẫu đã lưu và kích hoạt.
- [ ] Module kiểm tra có `reject_on_false` đúng chỗ.
- [ ] Điện thoại được kiểm tra bằng `phone_format` với `0` + 9 hoặc `+84` + 9.
- [ ] Số chuyến nằm trong khoảng cho phép.
- [ ] Object ảnh tham chiếu engine đang bật.
- [ ] Exports chỉ gửi đúng form, đúng trạng thái, đúng trường.
- [ ] Chạy thử export và kiểm tra báo cáo gần nhất.
- [ ] Tài khoản có đúng vai trò.
- [ ] Không đưa ảnh chụp có dữ liệu cá nhân vào tài liệu hoặc chat.

## 9. Xử lý sự cố nhanh

| Hiện tượng | Kiểm tra |
|---|---|
| Không đăng nhập được | Tài khoản còn hoạt động không, mật khẩu đúng không, tài khoản có bị xóa không |
| Không thấy menu Quản lý tài khoản | Tài khoản không phải superadmin |
| Form không nhận module | Module có được bật không, engine có tồn tại và đang bật không, input có đúng tên không |
| Bài nộp bị từ chối bởi module | Xem `reject_message`, kiểm tra giá trị thực tế và cấu hình module |
| Export không chạy | Kiểm tra cấu hình khách hàng, form được chọn, trạng thái/trường, báo cáo lỗi gần nhất |
| Object báo thiếu engine | Engine bị xóa hoặc bị tắt; bật lại trong **Module** hoặc sửa binding của object |
| Đổi ngôn ngữ không đổi nội dung | Nút `VI/EN` đổi ngôn ngữ thanh điều hướng và các màn hình đã có bản dịch; tải lại trang nếu cần |
