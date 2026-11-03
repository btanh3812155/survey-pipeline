CAMERA_FORM_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#090d16">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>Trust Bridge - Khảo Sát Phương Tiện & Doanh Thu GSM</title>
    <style>
        :root {
            --bg: #090d16;
            --surface: #131b2e;
            --surface-hover: #1c2742;
            --border: #23314f;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --text: #f8fafc;
            --muted: #94a3b8;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100dvh;
            display: flex;
            flex-direction: column;
            padding: env(safe-area-inset-top, 14px) 14px env(safe-area-inset-bottom, 20px) 14px;
            overflow-x: hidden;
            touch-action: manipulation;
        }

        header {
            text-align: center;
            padding: 10px 0 16px 0;
        }

        .badge-brand {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(37, 99, 235, 0.15);
            color: #60a5fa;
            border: 1px solid rgba(37, 99, 235, 0.3);
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }

        header h1 {
            font-size: 1.3rem;
            font-weight: 800;
            letter-spacing: -0.025em;
            color: #fff;
        }

        header p {
            font-size: 0.85rem;
            color: var(--muted);
            margin-top: 4px;
        }

        .form-card {
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 16px;
            padding: 18px 16px;
            margin-bottom: 16px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
        }

        .section-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
        }

        .section-title svg {
            width: 18px;
            height: 18px;
            fill: #60a5fa;
        }

        .input-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .input-group.full {
            grid-column: span 2;
        }

        .input-group label {
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--muted);
        }

        .input-group input, .input-group select {
            background-color: var(--bg);
            border: 1.5px solid var(--border);
            color: #fff;
            padding: 12px 14px;
            border-radius: 10px;
            font-size: 0.95rem;
            outline: none;
            transition: border-color 0.2s;
            width: 100%;
        }

        .input-group input:focus, .input-group select:focus {
            border-color: var(--accent);
        }

        /* Vehicle Type Segmented Control */
        .type-selector {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }

        .type-chip {
            background: var(--bg);
            border: 1.5px solid var(--border);
            border-radius: 10px;
            padding: 10px;
            text-align: center;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 600;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }

        .type-chip.selected {
            background: rgba(37, 99, 235, 0.15);
            border-color: var(--accent);
            color: #60a5fa;
        }

        /* Viewfinder Camera Section */
        .viewfinder-wrapper {
            position: relative;
            width: 100%;
            aspect-ratio: 4 / 3;
            max-height: 40dvh;
            background-color: #000;
            border-radius: 14px;
            overflow: hidden;
            border: 1.5px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        video {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .photo-preview {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: none;
        }

        .guide-box {
            position: absolute;
            top: 10%;
            left: 10%;
            right: 10%;
            bottom: 10%;
            border: 2px dashed rgba(255, 255, 255, 0.7);
            border-radius: 10px;
            pointer-events: none;
            display: flex;
            align-items: center;
            justify-content: center;
            text-align: center;
            background: rgba(0, 0, 0, 0.15);
        }

        .guide-text {
            color: #fff;
            font-size: 0.78rem;
            background: rgba(0, 0, 0, 0.7);
            padding: 5px 12px;
            border-radius: 20px;
            backdrop-filter: blur(4px);
        }

        .camera-actions {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 16px;
            padding: 8px 0;
        }

        .btn-shutter {
            width: 68px;
            height: 68px;
            border-radius: 50%;
            border: 4px solid #fff;
            background: linear-gradient(135deg, #10b981, #059669);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            box-shadow: 0 0 15px rgba(16, 185, 129, 0.4);
            transition: transform 0.1s;
        }

        .btn-shutter:active {
            transform: scale(0.92);
        }

        .btn-shutter svg {
            width: 30px;
            height: 30px;
            fill: #fff;
        }

        .btn-retake {
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: #fff;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            display: none;
            cursor: pointer;
        }

        /* Touchscreen Signature Pad */
        .signature-box {
            position: relative;
            background: #fff;
            border-radius: 12px;
            height: 120px;
            border: 1.5px solid var(--border);
            overflow: hidden;
            touch-action: none;
        }

        #signatureCanvas {
            width: 100%;
            height: 100%;
            display: block;
        }

        .sig-placeholder {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            color: #94a3b8;
            font-size: 0.82rem;
            pointer-events: none;
        }

        .sig-clear-btn {
            align-self: flex-end;
            background: transparent;
            border: none;
            color: #60a5fa;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
        }

        /* Decree 13 Consent Checkbox */
        .consent-group {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            font-size: 0.78rem;
            color: var(--muted);
            line-height: 1.4;
            padding: 8px;
            background: rgba(15, 23, 42, 0.6);
            border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.05);
        }

        .consent-group input[type="checkbox"] {
            width: 18px;
            height: 18px;
            accent-color: var(--accent);
            margin-top: 2px;
            flex-shrink: 0;
        }

        /* Submit Button */
        .btn-submit {
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            color: #fff;
            border: none;
            padding: 16px;
            border-radius: 12px;
            font-size: 1.05rem;
            font-weight: 700;
            width: 100%;
            cursor: pointer;
            box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: opacity 0.2s, transform 0.1s;
        }

        .btn-submit:active {
            transform: scale(0.98);
        }

        .btn-submit:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }

        /* Verdict Result Modal Card */
        .verdict-card {
            display: none;
            border-radius: 16px;
            padding: 20px 16px;
            text-align: center;
            margin-bottom: 20px;
            animation: fadeIn 0.3s ease;
        }

        .verdict-card.approved {
            display: block;
            background: rgba(16, 185, 129, 0.15);
            border: 2px solid var(--success);
        }

        .verdict-card.rejected {
            display: block;
            background: rgba(239, 68, 68, 0.15);
            border: 2px solid var(--danger);
        }

        .verdict-title {
            font-size: 1.2rem;
            font-weight: 800;
            margin-bottom: 6px;
        }

        .verdict-card.approved .verdict-title { color: #34d399; }
        .verdict-card.rejected .verdict-title { color: #f87171; }

        .verdict-desc {
            font-size: 0.88rem;
            color: #e2e8f0;
            line-height: 1.4;
            margin-bottom: 12px;
        }

        .verdict-details {
            background: rgba(0, 0, 0, 0.3);
            border-radius: 8px;
            padding: 10px;
            font-size: 0.8rem;
            text-align: left;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body>
    <header>
        <span class="badge-brand">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="#60a5fa"><path d="M12 2L1 21h22L12 2zm0 3.99L19.53 19H4.47L12 5.99zM11 16h2v2h-2zm0-6h2v4h-2z"/></svg>
            Trust Bridge Production
        </span>
        <h1>Phiếu Khảo Sát & Xác Thực Xe</h1>
        <p>Hệ thống chụp ảnh trực tiếp chống gian lận & kiểm tra AI</p>
    </header>

    <div id="verdictCard" class="verdict-card">
        <h2 id="verdictTitle" class="verdict-title"></h2>
        <p id="verdictDesc" class="verdict-desc"></p>
        <div id="verdictDetails" class="verdict-details"></div>
    </div>

    <form id="surveyForm" onsubmit="return false;">
        <!-- Card 1: Thông tin tài xế -->
        <div class="form-card">
            <div class="section-title">
                <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                1. Thông tin Tài xế & Phương tiện
            </div>

            <div class="input-row">
                <div class="input-group full">
                    <label for="driverPhone">Số điện thoại tài xế *</label>
                    <input type="tel" id="driverPhone" placeholder="Ví dụ: 0988123456" required autocomplete="tel">
                </div>

                <div class="input-group">
                    <label for="driverName">Họ và tên</label>
                    <input type="text" id="driverName" placeholder="Nguyễn Văn An">
                </div>

                <div class="input-group">
                    <label for="licensePlate">Biển số xe *</label>
                    <input type="text" id="licensePlate" placeholder="29E-123.45" required style="text-transform: uppercase;">
                </div>
            </div>

            <div class="input-group">
                <label>Loại phương tiện</label>
                <div class="type-selector">
                    <div class="type-chip selected" id="chipCar" onclick="selectType('CAR')">
                        🚗 Ô tô điện GSM
                    </div>
                    <div class="type-chip" id="chipBike" onclick="selectType('BIKE')">
                        🏍️ Xe máy điện
                    </div>
                </div>
                <input type="hidden" id="vehicleType" value="CAR">
            </div>
        </div>

        <!-- Card 2: Ca làm việc & Doanh thu -->
        <div class="form-card">
            <div class="section-title">
                <svg viewBox="0 0 24 24"><path d="M11.8 10.9c-2.27-.59-3-1.2-3-2.15 0-1.09 1.01-1.85 2.7-1.85 1.78 0 2.44.85 2.5 2.1h2.21c-.07-1.72-1.12-3.3-3.21-3.81V3h-3v2.16c-1.94.42-3.5 1.68-3.5 3.61 0 2.31 1.91 3.46 4.7 4.13 2.5.6 3 1.48 3 2.41 0 .69-.49 1.79-2.7 1.79-2.06 0-2.87-.92-2.98-2.1h-2.2c.12 2.19 1.76 3.42 3.68 3.83V21h3v-2.15c1.95-.37 3.5-1.5 3.5-3.55 0-2.84-2.43-3.81-4.7-4.4z"/></svg>
                2. Báo cáo Doanh thu & Ca làm việc
            </div>

            <div class="input-row">
                <div class="input-group">
                    <label for="grossRevenue">Tổng doanh thu (VNĐ)</label>
                    <input type="number" id="grossRevenue" placeholder="1.250.000" min="0">
                </div>

                <div class="input-group">
                    <label for="tripCount">Số cuốc hoàn thành</label>
                    <input type="number" id="tripCount" placeholder="18" min="0">
                </div>

                <div class="input-group full">
                    <label for="odometerKm">Số km công-tơ-mét (Odometer)</label>
                    <input type="number" id="odometerKm" placeholder="Ví dụ: 45230">
                </div>
            </div>
        </div>

        <!-- Card 3: Chụp ảnh hóa đơn / Xe (Live Camera Sensor) -->
        <div class="form-card">
            <div class="section-title">
                <svg viewBox="0 0 24 24"><path d="M12 9c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3m0-7C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8"/></svg>
                3. Chụp Hóa đơn / Đồng hồ Xe Trực tiếp *
            </div>

            <div class="viewfinder-wrapper" id="viewfinder">
                <video id="videoElement" autoplay playsinline muted></video>
                <img id="photoPreview" class="photo-preview" alt="Ảnh chụp">
                <div class="guide-box" id="guideBox">
                    <span class="guide-text">Căn chỉnh hóa đơn / xe vào khung</span>
                </div>
            </div>

            <div class="camera-actions">
                <button type="button" class="btn-shutter" id="shutterBtn" aria-label="Chụp ảnh">
                    <svg viewBox="0 0 24 24"><path d="M12 9c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3m0-7C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8"/></svg>
                </button>
                <button type="button" class="btn-retake" id="retakeBtn" onclick="retakePhoto()">🔄 Chụp lại</button>
            </div>
        </div>

        <!-- Card 4: Chữ ký điện tử & Cam kết Nghị định 13 -->
        <div class="form-card">
            <div class="section-title" style="justify-content: space-between;">
                <span style="display:flex; align-items:center; gap:8px;">
                    <svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                    4. Chữ ký Tài xế & Cam kết
                </span>
                <button type="button" class="sig-clear-btn" onclick="clearSignature()">Xóa ký lại</button>
            </div>

            <div class="signature-box" id="signatureBox">
                <canvas id="signatureCanvas"></canvas>
                <span class="sig-placeholder" id="sigPlaceholder">Dùng ngón tay ký tên vào đây</span>
            </div>

            <label class="consent-group">
                <input type="checkbox" id="consentCheck" checked required>
                <span>Tôi cam đoan thông tin trên là chính xác và đồng ý cho Công ty thu thập, xử lý ảnh phương tiện và vị trí GPS theo <b>Nghị định 13/2023/NĐ-CP</b>.</span>
            </label>
        </div>

        <button type="button" class="btn-submit" id="submitBtn" onclick="submitSurvey()">
            <span>🚀 Gửi Phiếu Khảo Sát</span>
        </button>
    </form>

    <canvas id="captureCanvas" style="display:none;"></canvas>

    <script>
        // State variables
        let capturedBlob = null;
        let activeStream = null;
        let isSigning = false;
        let hasSigned = false;

        // Elements
        const video = document.getElementById('videoElement');
        const photoPreview = document.getElementById('photoPreview');
        const guideBox = document.getElementById('guideBox');
        const shutterBtn = document.getElementById('shutterBtn');
        const retakeBtn = document.getElementById('retakeBtn');
        const submitBtn = document.getElementById('submitBtn');
        const canvas = document.getElementById('captureCanvas');
        const sigCanvas = document.getElementById('signatureCanvas');
        const sigCtx = sigCanvas.getContext('2d');
        const sigPlaceholder = document.getElementById('sigPlaceholder');

        // Init camera
        async function initCamera() {
            try {
                const constraints = {
                    video: {
                        facingMode: { ideal: "environment" },
                        width: { ideal: 1920 },
                        height: { ideal: 1080 }
                    },
                    audio: false
                };
                activeStream = await navigator.mediaDevices.getUserMedia(constraints);
                video.srcObject = activeStream;
            } catch (err) {
                console.warn("Hardware camera unavailable:", err);
                guideBox.querySelector('.guide-text').textContent = "Không mở được camera tự động";
            }
        }

        // Init signature pad
        function initSignaturePad() {
            sigCanvas.width = sigCanvas.parentElement.clientWidth;
            sigCanvas.height = sigCanvas.parentElement.clientHeight;
            sigCtx.strokeStyle = "#0f172a";
            sigCtx.lineWidth = 2.5;
            sigCtx.lineCap = "round";

            function getPos(e) {
                const rect = sigCanvas.getBoundingClientRect();
                const clientX = e.touches ? e.touches[0].clientX : e.clientX;
                const clientY = e.touches ? e.touches[0].clientY : e.clientY;
                return { x: clientX - rect.left, y: clientY - rect.top };
            }

            function startDraw(e) {
                isSigning = true;
                hasSigned = true;
                sigPlaceholder.style.display = 'none';
                const pos = getPos(e);
                sigCtx.beginPath();
                sigCtx.moveTo(pos.x, pos.y);
            }

            function moveDraw(e) {
                if (!isSigning) return;
                const pos = getPos(e);
                sigCtx.lineTo(pos.x, pos.y);
                sigCtx.stroke();
            }

            function stopDraw() { isSigning = false; }

            sigCanvas.addEventListener('mousedown', startDraw);
            sigCanvas.addEventListener('mousemove', moveDraw);
            window.addEventListener('mouseup', stopDraw);

            sigCanvas.addEventListener('touchstart', (e) => { e.preventDefault(); startDraw(e); }, { passive: false });
            sigCanvas.addEventListener('touchmove', (e) => { e.preventDefault(); moveDraw(e); }, { passive: false });
            window.addEventListener('touchend', stopDraw);
        }

        function clearSignature() {
            sigCtx.clearRect(0, 0, sigCanvas.width, sigCanvas.height);
            sigPlaceholder.style.display = 'block';
            hasSigned = false;
        }

        function selectType(type) {
            document.getElementById('vehicleType').value = type;
            document.getElementById('chipCar').classList.toggle('selected', type === 'CAR');
            document.getElementById('chipBike').classList.toggle('selected', type === 'BIKE');
        }

        // Snapshot handler
        shutterBtn.addEventListener('click', () => {
            if (!video.videoWidth) return;
            if (navigator.vibrate) navigator.vibrate(50);

            // Scale to 1280px max
            const scale = Math.min(1.0, 1280 / Math.max(video.videoWidth, video.videoHeight));
            canvas.width = Math.round(video.videoWidth * scale);
            canvas.height = Math.round(video.videoHeight * scale);
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

            canvas.toBlob((blob) => {
                capturedBlob = blob;
                photoPreview.src = URL.createObjectURL(blob);
                photoPreview.style.display = 'block';
                video.style.display = 'none';
                guideBox.style.display = 'none';
                shutterBtn.style.display = 'none';
                retakeBtn.style.display = 'inline-block';
            }, 'image/webp', 0.82);
        });

        function retakePhoto() {
            capturedBlob = null;
            photoPreview.style.display = 'none';
            video.style.display = 'block';
            guideBox.style.display = 'flex';
            shutterBtn.style.display = 'flex';
            retakeBtn.style.display = 'none';
        }

        // Full submission
        async function submitSurvey() {
            const phone = document.getElementById('driverPhone').value.trim();
            const plate = document.getElementById('licensePlate').value.trim().toUpperCase();

            if (!phone) {
                alert("Vui lòng nhập số điện thoại tài xế!");
                document.getElementById('driverPhone').focus();
                return;
            }

            if (!plate) {
                alert("Vui lòng nhập biển số xe!");
                document.getElementById('licensePlate').focus();
                return;
            }

            if (!capturedBlob) {
                alert("Vui lòng chụp ảnh hóa đơn hoặc phương tiện trước khi gửi!");
                return;
            }

            if (!document.getElementById('consentCheck').checked) {
                alert("Vui lòng đồng ý với điều khoản Nghị định 13 để tiếp tục.");
                return;
            }

            submitBtn.disabled = true;
            submitBtn.innerHTML = `<span>⏳ Đang gửi & AI thẩm định...</span>`;

            // Prepare multipart payload
            const formData = new FormData();
            formData.append('survey_photo', capturedBlob, 'survey_photo.webp');
            formData.append('driver_phone', phone);
            formData.append('driver_name', document.getElementById('driverName').value.trim());
            formData.append('license_plate', plate);
            formData.append('vehicle_type', document.getElementById('vehicleType').value);
            formData.append('gross_revenue', document.getElementById('grossRevenue').value || '0');
            formData.append('trip_count', document.getElementById('tripCount').value || '0');
            formData.append('odometer_km', document.getElementById('odometerKm').value || '0');

            if (hasSigned) {
                formData.append('signature_png', sigCanvas.toDataURL('image/png'));
            }

            try {
                const resp = await fetch('/api/v1/survey/submit-webrtc', {
                    method: 'POST',
                    body: formData
                });
                const res = await resp.json();

                const card = document.getElementById('verdictCard');
                const title = document.getElementById('verdictTitle');
                const desc = document.getElementById('verdictDesc');
                const details = document.getElementById('verdictDetails');

                if (res.status === 'APPROVED') {
                    card.className = "verdict-card approved";
                    title.textContent = "✅ KHẢO SÁT HỢP LỆ!";
                    desc.textContent = `Hệ thống đã phê duyệt phiếu #${res.submission_id}. Dữ liệu đã được ghi nhận vào sổ cái tài chính.`;
                } else {
                    card.className = "verdict-card rejected";
                    title.textContent = "❌ KHẢO SÁT CHƯA HỢP LỆ";
                    desc.textContent = res.reason || "Ảnh không đủ độ nét hoặc lệch thông tin ca xe.";
                }

                details.innerHTML = `
                    <div>• <b>Mã phiếu:</b> #${res.submission_id}</div>
                    <div>• <b>Biển số:</b> ${plate} (${res.detected_plate ? 'AI phát hiện: ' + res.detected_plate : 'Đã đối soát'})</div>
                    <div>• <b>Tài xế:</b> ${phone}</div>
                    <div>• <b>Thời gian duyệt:</b> ${new Date().toLocaleTimeString()}</div>
                `;

                card.scrollIntoView({ behavior: 'smooth' });
            } catch (err) {
                alert("Lỗi kết nối máy chủ! Vui lòng thử lại.");
            } finally {
                submitBtn.disabled = false;
                submitBtn.innerHTML = `<span>🚀 Gửi Phiếu Khảo Sát Khác</span>`;
            }
        }

        window.addEventListener('DOMContentLoaded', () => {
            initCamera();
            initSignaturePad();
        });
    </script>
</body>
</html>
"""
