import json
from services.form_engine import FormSchema

def render_dynamic_form_html(schema: FormSchema) -> str:
    schema_json = schema.model_dump_json()

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#090d16">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>{schema.title}</title>
    <style>
        :root {{
            --bg: #090d16;
            --surface: #131b2e;
            --surface-hover: #1c2742;
            --border: #23314f;
            --accent: #2563eb;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --text: #f8fafc;
            --muted: #94a3b8;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }}

        html {{
            overflow-y: scroll;
            -webkit-overflow-scrolling: touch;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100%;
            display: flex;
            flex-direction: column;
            padding: env(safe-area-inset-top, 12px) 14px env(safe-area-inset-bottom, 24px) 14px;
            overflow-x: hidden;
            overflow-y: visible;
        }}

        /* Top Nav Bar with Globe Language Switcher */
        .top-nav-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}

        .btn-lang-toggle {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(30, 41, 59, 0.85);
            border: 1px solid var(--border);
            color: #f8fafc;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 0.82rem;
            font-weight: 700;
            cursor: pointer;
            backdrop-filter: blur(8px);
        }}

        .globe-icon {{
            width: 16px;
            height: 16px;
            fill: #60a5fa;
        }}

        /* GPS Authorization Banner */
        .gps-banner {{
            background: rgba(30, 41, 59, 0.95);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 14px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.82rem;
            backdrop-filter: blur(8px);
        }}

        .gps-status-text {{
            font-weight: 600;
            color: #cbd5e1;
        }}

        .btn-gps {{
            background: var(--accent);
            color: #fff;
            border: none;
            padding: 6px 14px;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 700;
            cursor: pointer;
        }}

        /* Test Mode Banner */
        .test-banner {{
            background: rgba(30, 41, 59, 0.6);
            border: 1.5px solid rgba(255, 255, 255, 0.12);
            border-radius: 12px;
            padding: 10px 14px;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.82rem;
            color: #e2e8f0;
        }}

        .btn-test-toggle {{
            background: #334155;
            color: #fff;
            border: none;
            padding: 5px 12px;
            border-radius: 6px;
            font-weight: 700;
            cursor: pointer;
            font-size: 0.78rem;
        }}

        header {{
            text-align: center;
            margin-bottom: 16px;
        }}

        header h1 {{
            font-size: 1.25rem;
            font-weight: 800;
            color: #fff;
            letter-spacing: -0.02em;
        }}

        header p {{
            font-size: 0.85rem;
            color: var(--muted);
            margin-top: 4px;
        }}

        /* Question Cards */
        .question-card {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            transition: all 0.3s ease;
        }}

        .question-card.hidden {{
            display: none !important;
        }}

        .q-header {{
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .q-title-row {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 8px;
        }}

        .q-title {{
            font-size: 0.95rem;
            font-weight: 700;
            color: #fff;
            line-height: 1.35;
        }}

        .q-required {{
            color: var(--danger);
            font-weight: 700;
            margin-left: 2px;
        }}

        .q-desc {{
            font-size: 0.8rem;
            color: var(--muted);
            line-height: 1.4;
        }}

        .btn-sample {{
            align-self: flex-start;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: #93c5fd;
            font-size: 0.75rem;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 6px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            margin-top: 2px;
        }}

        .sample-preview-box {{
            display: none;
            background: rgba(15, 23, 42, 0.7);
            border-left: 3px solid #60a5fa;
            border-radius: 4px;
            padding: 10px;
            font-size: 0.8rem;
            color: #e2e8f0;
            margin-top: 6px;
        }}

        .q-input {{
            background: var(--bg);
            border: 1.5px solid var(--border);
            color: #fff;
            padding: 12px 14px;
            border-radius: 10px;
            font-size: 0.95rem;
            outline: none;
            width: 100%;
        }}

        .q-input:focus {{
            border-color: var(--accent);
        }}

        /* Choices */
        .choices-list {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        .choice-item {{
            background: var(--bg);
            border: 1.5px solid var(--border);
            border-radius: 10px;
            padding: 12px 14px;
            display: flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            transition: all 0.15s ease;
        }}

        .choice-item.selected {{
            border-color: var(--accent);
            background: rgba(37, 99, 235, 0.15);
            color: #93c5fd;
            font-weight: 600;
        }}

        /* Fixed-Height 4:3 Aspect-Ratio Camera Viewport (Never Changes Layout Height!) */
        .camera-fixed-viewport {{
            position: relative;
            width: 100%;
            aspect-ratio: 4 / 3;
            max-height: 42dvh;
            background: #0f172a;
            border-radius: 12px;
            overflow: hidden;
            border: 1.5px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .cam-standby-placeholder {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 12px;
            padding: 20px;
            text-align: center;
            width: 100%;
            height: 100%;
            background: #0f172a;
        }}

        .cam-crossed-icon {{
            width: 48px;
            height: 48px;
            fill: #64748b;
        }}

        .cam-off-label {{
            font-size: 0.88rem;
            color: #94a3b8;
            font-weight: 600;
        }}

        .btn-cam-activate {{
            background: var(--accent);
            color: #fff;
            border: none;
            padding: 10px 20px;
            border-radius: 10px;
            font-weight: 700;
            font-size: 0.88rem;
            cursor: pointer;
        }}

        video {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: none;
        }}

        .captured-img {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: none;
        }}

        .camera-overlay {{
            position: absolute;
            top: 10%; left: 10%; right: 10%; bottom: 10%;
            border: 2px dashed rgba(255, 255, 255, 0.7);
            border-radius: 8px;
            pointer-events: none;
            display: none;
            align-items: center;
            justify-content: center;
        }}

        .btn-camera-snap {{
            width: 68px;
            height: 68px;
            border-radius: 50%;
            border: 4px solid #fff;
            background: linear-gradient(135deg, #10b981, #059669);
            margin: 10px auto 0 auto;
            display: none;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: opacity 0.15s, filter 0.15s;
        }}

        /* Greyed out during retake delay */
        .btn-camera-snap.disabled-delay {{
            opacity: 0.35 !important;
            filter: grayscale(1) !important;
            pointer-events: none !important;
        }}

        .btn-camera-retake {{
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: #fff;
            padding: 8px 18px;
            border-radius: 8px;
            font-size: 0.85rem;
            display: none;
            margin: 10px auto 0 auto;
            cursor: pointer;
            font-weight: 600;
        }}

        .btn-cam-deactivate {{
            background: transparent;
            color: #94a3b8;
            border: 1px solid var(--border);
            padding: 6px 14px;
            border-radius: 8px;
            font-size: 0.78rem;
            cursor: pointer;
            display: none;
            margin: 8px auto 0 auto;
        }}

        /* Signature Question Trigger Card (Full Width, Aesthetic Warm Cream Paper) */
        .sig-preview-card {{
            color-scheme: light !important;
            background-color: #ffffff !important;
            border: 1.5px solid #cbd5e1 !important;
            border-radius: 12px;
            padding: 24px 16px;
            width: 100% !important;
            min-height: 140px;
            text-align: center;
            cursor: pointer;
            color: #0f172a !important;
            box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.05), 0 2px 8px rgba(0,0,0,0.25);
            background-image: linear-gradient(rgba(203, 213, 225, 0.45) 1px, transparent 1px) !important;
            background-size: 100% 28px !important;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 10px;
            transition: transform 0.1s, border-color 0.2s;
        }}

        .sig-preview-card * {{
            color: #0f172a !important;
        }}

        .sig-preview-img {{
            width: 100%;
            max-height: 90px;
            object-fit: contain;
            display: none;
            border-radius: 6px;
            border: 1px solid #cbd5e1;
            background: #ffffff;
        }}

        /* Paper Signature Modal Popup */
        .modal-paper-overlay {{
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            width: 100vw;
            height: 100dvh;
            background: rgba(0, 0, 0, 0.88);
            backdrop-filter: blur(8px);
            z-index: 99999;
            align-items: center;
            justify-content: center;
            padding: 14px;
            touch-action: none !important;
        }}

        .paper-sheet {{
            color-scheme: light !important;
            background-color: #fdfbf7 !important;
            color: #0f172a !important;
            width: 100%;
            max-width: 480px;
            border-radius: 16px;
            padding: 20px 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.85);
            border: 1.5px solid #dcdad2;
            border-top: 6px solid #2563eb;
            touch-action: none !important;
        }}

        .paper-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .paper-header h3 {{
            font-size: 1.05rem;
            font-weight: 800;
            color: #0f172a !important;
        }}

        .paper-canvas-wrap {{
            color-scheme: light !important;
            position: relative;
            background-color: #fdfbf7 !important;
            border: 1.5px solid #cbd5e1;
            border-radius: 10px;
            height: 220px;
            touch-action: none !important;
            overflow: hidden;
            background-image: linear-gradient(rgba(203, 213, 225, 0.4) 1px, transparent 1px);
            background-size: 100% 28px;
        }}

        #modalSigCanvas {{
            width: 100%;
            height: 100%;
            display: block;
            touch-action: none !important;
            -webkit-touch-callout: none !important;
            user-select: none !important;
            -webkit-user-select: none !important;
        }}

        .sig-baseline {{
            position: absolute;
            bottom: 45px;
            left: 20px;
            right: 20px;
            border-bottom: 1.5px dashed #94a3b8;
            pointer-events: none;
        }}

        .sig-baseline-text {{
            position: absolute;
            bottom: 22px;
            left: 20px;
            font-size: 0.72rem;
            color: #94a3b8;
            pointer-events: none;
            font-style: italic;
        }}

        .paper-footer {{
            display: flex;
            justify-content: space-between;
            gap: 10px;
        }}

        .btn-modal {{
            flex: 1;
            padding: 12px;
            border-radius: 10px;
            font-weight: 700;
            font-size: 0.9rem;
            cursor: pointer;
            border: none;
        }}

        .btn-modal.cancel {{ background: #e2e8f0; color: #475569; }}
        .btn-modal.clear {{ background: #fef3c7; color: #b45309; }}
        .btn-modal.confirm {{ background: #2563eb; color: #fff; }}

        /* Confirmation Dialog Modal */
        .confirm-modal-overlay {{
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            z-index: 10000;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}

        .confirm-box {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 16px;
            padding: 20px;
            max-width: 380px;
            text-align: center;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}

        /* Buttons & Actions */
        .btn-submit {{
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            color: #fff;
            border: none;
            padding: 16px;
            border-radius: 12px;
            font-size: 1.05rem;
            font-weight: 700;
            width: 100%;
            cursor: pointer;
            margin-top: 10px;
        }}

        .btn-floating-reset {{
            position: fixed;
            bottom: calc(env(safe-area-inset-bottom, 20px) + 16px);
            right: 16px;
            z-index: 1000;
            background: rgba(30, 41, 59, 0.9);
            border: 1.5px solid rgba(239, 68, 68, 0.4);
            color: #f87171;
            padding: 9px 18px;
            border-radius: 24px;
            font-size: 0.8rem;
            font-weight: 700;
            cursor: pointer;
            backdrop-filter: blur(8px);
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
            transition: all 0.2s;
        }}

        .decree13-notice {{
            font-size: 0.76rem;
            color: var(--muted);
            text-align: center;
            margin-top: 14px;
            line-height: 1.4;
        }}
    </style>
</head>
<body>
    <!-- Top Nav with Globe Button -->
    <div class="top-nav-bar">
        <button type="button" class="btn-lang-toggle" onclick="toggleLanguage()" aria-label="Toggle Language">
            <svg viewBox="0 0 24 24" class="globe-icon"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
            <span id="currentLangCode">VI</span>
        </button>
        <button type="button" class="btn-sample" onclick="openHistoryModal()" id="btnHistoryNav">Xem lịch sử nộp</button>
    </div>

    <!-- GPS Authorization Banner -->
    <div class="gps-banner" id="gpsBanner">
        <div class="gps-status-text" id="gpsStatusText">
            Cho phép truy cập vị trí để xác nhận điểm bàn giao ca
        </div>
        <button type="button" class="btn-gps" id="btnRequestGps" onclick="requestGps()">Bật vị trí</button>
    </div>

    <!-- Test Mode Banner with Bypass Toggle -->
    <div class="test-banner" id="testModeBanner">
        <div style="display:flex; flex-direction:column; gap:4px;">
            <span><span id="txtModeLabel">Chế độ biểu mẫu:</span> <b id="testModeStatus">Chính thức</b></span>
            <label id="bypassCheckLabel" style="display:none; align-items:center; gap:6px; font-size:0.75rem; color:#fde68a; cursor:pointer;">
                <input type="checkbox" id="bypassImgToggle" onchange="toggleBypassImg(this.checked)">
                <span id="txtBypassLabel">Bỏ qua kiểm tra ảnh (Test nhanh dữ liệu)</span>
            </label>
        </div>
        <button type="button" class="btn-test-toggle" id="btnToggleTestMode" onclick="toggleTestMode()">Bật thử nghiệm</button>
    </div>

    <header>
        <h1 id="headerTitle">{schema.title}</h1>
        <p id="headerDesc">{schema.description or ''}</p>
    </header>

    <form id="dynamicForm" onsubmit="return false;">
        <div id="questionsContainer"></div>

        <button type="button" class="btn-submit" id="btnSubmitForm" onclick="handleSubmit()">
            <span id="txtSubmitBtn">Gửi Báo Cáo</span>
        </button>

        <!-- Floating Reset Button Overlay -->
        <button type="button" class="btn-floating-reset" onclick="promptResetForm()" aria-label="Reset">
            <span id="txtResetFloatBtn">Làm lại</span>
        </button>

        <p class="decree13-notice" id="txtDecree13Footer">
            Thông tin báo cáo ca xe được lưu trữ bảo mật nội bộ.
        </p>
    </form>

    <!-- Paper Signature Modal -->
    <div class="modal-paper-overlay" id="paperModal">
        <div class="paper-sheet">
            <div class="paper-header">
                <h3 id="txtSigModalTitle">Bảng Ký Tên Bàn Giao Ca</h3>
                <span style="font-size: 0.8rem; color: #64748b;" id="txtSigModalSub">Dùng ngón tay ký tên</span>
            </div>
            <div class="paper-canvas-wrap">
                <canvas id="modalSigCanvas"></canvas>
                <div class="sig-baseline"></div>
                <div class="sig-baseline-text" id="txtSigBaselineHint">Ký tên phía trên đường kẻ</div>
            </div>
            <div class="paper-footer">
                <button type="button" class="btn-modal cancel" onclick="closeSigModal()" id="btnSigClose">Đóng</button>
                <button type="button" class="btn-modal clear" onclick="clearModalSig()" id="btnSigClear">Ký lại</button>
                <button type="button" class="btn-modal confirm" onclick="confirmModalSig()" id="btnSigConfirm">Hoàn tất chữ ký</button>
            </div>
        </div>
    </div>

    <!-- Reset Confirmation Modal -->
    <div class="confirm-modal-overlay" id="confirmResetModal">
        <div class="confirm-box">
            <h3 style="color:#ef4444; font-size:1.1rem;" id="txtResetConfirmTitle">Làm lại từ đầu?</h3>
            <p style="font-size:0.85rem; color:#cbd5e1;" id="txtResetConfirmDesc">Toàn bộ thông tin vừa nhập sẽ bị xóa để bạn nhập lại.</p>
            <div style="display:flex; gap:10px;">
                <button type="button" style="flex:1; padding:10px; border-radius:8px; border:none; background:#334155; color:#fff;" onclick="closeResetModal()" id="btnResetCancel">Hủy</button>
                <button type="button" style="flex:1; padding:10px; border-radius:8px; border:none; background:#ef4444; color:#fff; font-weight:700;" onclick="executeResetForm()" id="btnResetConfirm">Đồng ý xóa</button>
            </div>
        </div>
    </div>

    <!-- History Modal -->
    <div class="modal-paper-overlay" id="historyModal" style="z-index:99998;">
        <div class="paper-sheet" style="background:#0f172a !important; color:#f8fafc !important; border:1px solid #334155; border-top:6px solid #2563eb;">
            <div class="paper-header">
                <h3 style="color:#f8fafc !important;" id="txtHistoryModalTitle">Lịch Sử Các Lần Nộp</h3>
                <button type="button" class="btn-modal cancel" style="flex:0 0 auto; padding:6px 12px; font-size:0.8rem;" onclick="closeHistoryModal()" id="btnHistoryClose">Đóng</button>
            </div>
            <div style="display:flex; gap:8px;">
                <input type="tel" id="historyPhoneInput" class="q-input" placeholder="Nhập số điện thoại để tra cứu...">
                <button type="button" class="btn" style="flex-shrink:0; padding:10px 14px; background:var(--accent); color:#fff; border:none; border-radius:8px; font-weight:700;" onclick="fetchHistoryAttempts()" id="btnHistoryQuery">Tra cứu</button>
            </div>
            <div id="historyResultsList" style="max-height:260px; overflow-y:auto; display:flex; flex-direction:column; gap:8px; font-size:0.8rem; margin-top:8px;">
                <div style="color:#94a3b8; text-align:center; padding:12px;" id="txtHistoryPrompt">Nhập số điện thoại và bấm Tra cứu để xem các lần nộp trước.</div>
            </div>
        </div>
    </div>

    <canvas id="hiddenCameraCanvas" style="display:none;"></canvas>

    <script>
        const SCHEMA = {schema_json};
        const answers = {{}};
        let gpsCoordinates = null;
        let activeVideoStream = null;
        let activeSignatureTargetQId = null;

        // Bilingual Translations
        const I18N = {{
            vi: {{
                code: "VI",
                gps_prompt: "Cho phép truy cập vị trí để xác nhận điểm bàn giao ca",
                gps_btn: "Bật vị trí",
                gps_getting: "Đang xác định vị trí...",
                gps_denied: "Quyền vị trí bị từ chối. Vui lòng cho phép trong cài đặt.",
                gps_err: "Chưa thể lấy vị trí. Vui lòng thử lại.",
                mode_label: "Chế độ biểu mẫu:",
                mode_prod: "Chính thức",
                mode_test: "Thử nghiệm (Không lưu hệ thống)",
                btn_to_test: "Bật thử nghiệm",
                btn_to_prod: "Về Chính thức",
                bypass_label: "Bỏ qua kiểm tra ảnh (Test nhanh dữ liệu)",
                btn_history_nav: "Xem lịch sử nộp",
                sig_prompt: "Bấm vào đây để ký tên trên giấy",
                sig_done: "Chữ ký đã ghi nhận (Bấm vào đây để ký lại)",
                sig_title: "Bảng Ký Tên Bàn Giao Ca",
                sig_sub: "Dùng ngón tay ký tên",
                sig_hint: "Ký tên phía trên đường kẻ",
                btn_close: "Đóng",
                btn_clear: "Ký lại",
                btn_confirm: "Hoàn tất chữ ký",
                btn_submit: "Gửi Báo Cáo",
                btn_submitting: "Đang gửi báo cáo...",
                btn_reset: "Làm lại",
                reset_title: "Làm lại từ đầu?",
                reset_desc: "Toàn bộ thông tin vừa nhập sẽ bị xóa để bạn nhập lại.",
                btn_cancel: "Hủy",
                btn_confirm_reset: "Đồng ý xóa",
                reset_done: "Biểu mẫu đã được làm lại từ đầu!",
                cam_off: "Camera đang tắt",
                btn_cam_on: "Bật Camera",
                btn_cam_off: "Tắt Camera",
                cam_hint: "Căn chỉnh đồng hồ xe hoặc hóa đơn vào khung",
                btn_retake: "Chụp lại",
                btn_sample_img: "Xem ảnh mẫu",
                btn_sample_ans: "Xem câu trả lời mẫu",
                decree_footer: "Thông tin báo cáo ca xe được lưu trữ bảo mật nội bộ.",
                history_title: "Lịch Sử Các Lần Nộp",
                history_ph: "Nhập số điện thoại để tra cứu...",
                history_query: "Tra cứu",
                history_prompt: "Nhập số điện thoại và bấm Tra cứu để xem các lần nộp trước.",
                history_empty: "Chưa có lần nộp nào được ghi nhận cho số điện thoại này.",
                missing_required: "Vui lòng trả lời câu hỏi bắt buộc: ",
                verdict_approved: "BÁO CÁO HỢP LỆ! Bàn giao ca thành công.",
                verdict_rejected: "CẦN KIỂM TRA LẠI!",
                server_err: "Lỗi kết nối máy chủ. Vui lòng thử lại sau."
            }},
            en: {{
                code: "EN",
                gps_prompt: "Allow location access to verify vehicle shift handover location",
                gps_btn: "Enable Location",
                gps_getting: "Detecting location...",
                gps_denied: "Location permission denied. Please allow in settings.",
                gps_err: "Cannot obtain location. Please try again.",
                mode_label: "Form Mode:",
                mode_prod: "Production",
                mode_test: "Test Mode (Dev Storage)",
                btn_to_test: "Switch to Test",
                btn_to_prod: "Switch to Prod",
                bypass_label: "Bypass image audit (Fast test data flow)",
                btn_history_nav: "View History",
                sig_prompt: "Tap here to sign on paper",
                sig_done: "Signature recorded (Tap to resign)",
                sig_title: "Driver Signature Sheet",
                sig_sub: "Sign with your finger",
                sig_hint: "Sign above the dashed line",
                btn_close: "Close",
                btn_clear: "Clear",
                btn_confirm: "Confirm Signature",
                btn_submit: "Submit Report",
                btn_submitting: "Submitting report...",
                btn_reset: "Reset",
                reset_title: "Reset from scratch?",
                reset_desc: "All entered information will be cleared.",
                btn_cancel: "Cancel",
                btn_confirm_reset: "Yes, Reset",
                reset_done: "Form has been reset from scratch!",
                cam_off: "Camera is off",
                btn_cam_on: "Turn on Camera",
                btn_cam_off: "Turn off Camera",
                cam_hint: "Align odometer or receipt inside frame",
                btn_retake: "Retake",
                btn_sample_img: "View Sample Photo",
                btn_sample_ans: "View Sample Answer",
                decree_footer: "Shift report data is encrypted and securely stored.",
                history_title: "Submission History",
                history_ph: "Enter phone number to look up...",
                history_query: "Search",
                history_prompt: "Enter phone number and click Search to view history.",
                history_empty: "No submissions found for this phone number.",
                missing_required: "Please answer required question: ",
                verdict_approved: "REPORT APPROVED! Shift handover succeeded.",
                verdict_rejected: "VERIFICATION FAILED!",
                server_err: "Server connection error. Please try again later."
            }}
        }};

        let currentLang = localStorage.getItem('tb_lang') || 'vi';

        function applyI18n() {{
            const t = I18N[currentLang];
            document.getElementById('currentLangCode').textContent = t.code;
            document.getElementById('gpsStatusText').textContent = t.gps_prompt;
            document.getElementById('btnRequestGps').textContent = t.gps_btn;
            document.getElementById('txtModeLabel').textContent = t.mode_label;
            document.getElementById('btnHistoryNav').textContent = t.btn_history_nav;
            document.getElementById('txtBypassLabel').textContent = t.bypass_label;
            document.getElementById('txtSubmitBtn').textContent = t.btn_submit;
            document.getElementById('txtResetFloatBtn').textContent = t.btn_reset;
            document.getElementById('txtDecree13Footer').textContent = t.decree_footer;

            document.getElementById('txtSigModalTitle').textContent = t.sig_title;
            document.getElementById('txtSigModalSub').textContent = t.sig_sub;
            document.getElementById('txtSigBaselineHint').textContent = t.sig_hint;
            document.getElementById('btnSigClose').textContent = t.btn_close;
            document.getElementById('btnSigClear').textContent = t.btn_clear;
            document.getElementById('btnSigConfirm').textContent = t.btn_confirm;

            document.getElementById('txtResetConfirmTitle').textContent = t.reset_title;
            document.getElementById('txtResetConfirmDesc').textContent = t.reset_desc;
            document.getElementById('btnResetCancel').textContent = t.btn_cancel;
            document.getElementById('btnResetConfirm').textContent = t.btn_confirm_reset;

            document.getElementById('txtHistoryModalTitle').textContent = t.history_title;
            document.getElementById('historyPhoneInput').placeholder = t.history_ph;
            document.getElementById('btnHistoryQuery').textContent = t.history_query;
            document.getElementById('btnHistoryClose').textContent = t.btn_close;

            updateTestModeUI();
        }}

        function toggleLanguage() {{
            currentLang = currentLang === 'vi' ? 'en' : 'vi';
            localStorage.setItem('tb_lang', currentLang);
            applyI18n();
            renderForm();
        }}

        // Client Idempotency Key
        let clientSubmissionId = 'IDEMP-' + Math.random().toString(36).substring(2, 10).toUpperCase();

        // Test Mode State
        let isTestMode = Boolean(SCHEMA.settings && SCHEMA.settings.is_test_mode) || window.location.search.includes('test=1') || window.location.search.includes('mode=test');
        let isBypassImg = false;

        function updateTestModeUI() {{
            const banner = document.getElementById('testModeBanner');
            const statusTxt = document.getElementById('testModeStatus');
            const toggleBtn = document.getElementById('btnToggleTestMode');
            const bypassLabel = document.getElementById('bypassCheckLabel');
            if (!banner || !statusTxt || !toggleBtn) return;

            const t = I18N[currentLang];
            if (isTestMode) {{
                statusTxt.textContent = t.mode_test;
                banner.style.background = "rgba(245, 158, 11, 0.2)";
                banner.style.borderColor = "#f59e0b";
                toggleBtn.textContent = t.btn_to_prod;
                if (bypassLabel) bypassLabel.style.display = 'flex';
            }} else {{
                statusTxt.textContent = t.mode_prod;
                banner.style.background = "rgba(30, 41, 59, 0.6)";
                banner.style.borderColor = "rgba(255, 255, 255, 0.12)";
                toggleBtn.textContent = t.btn_to_test;
                if (bypassLabel) bypassLabel.style.display = 'none';
            }}
        }}

        function toggleTestMode() {{
            isTestMode = !isTestMode;
            updateTestModeUI();
        }}

        function toggleBypassImg(checked) {{
            isBypassImg = checked;
            clientSubmissionId = 'IDEMP-' + Math.random().toString(36).substring(2, 10).toUpperCase();
            if (!checked) {{
                if (answers['camera_receipt'] && answers['camera_receipt'].isDummy) {{
                    delete answers['camera_receipt'];
                }}
            }}
        }}

        const container = document.getElementById('questionsContainer');
        const modal = document.getElementById('paperModal');
        const sigCanvas = document.getElementById('modalSigCanvas');
        const sigCtx = sigCanvas.getContext('2d');
        let isDrawing = false;
        let hasModalSignature = false;

        // 1. GPS Request
        function requestGps() {{
            if (!navigator.geolocation) {{
                alert("Geolocation not supported!");
                return;
            }}
            const btn = document.getElementById('btnRequestGps');
            const txt = document.getElementById('gpsStatusText');
            btn.disabled = true;
            btn.textContent = "...";
            txt.textContent = I18N[currentLang].gps_getting;

            navigator.geolocation.getCurrentPosition(
                (pos) => {{
                    gpsCoordinates = {{
                        latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude,
                        accuracy: pos.coords.accuracy
                    }};
                    const banner = document.getElementById('gpsBanner');
                    if (banner) banner.style.display = 'none';
                }},
                (err) => {{
                    btn.disabled = false;
                    btn.textContent = I18N[currentLang].gps_btn;
                    txt.textContent = (err.code === 1) ? I18N[currentLang].gps_denied : I18N[currentLang].gps_err;
                }},
                {{ enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 }}
            );
        }}

        // 2. Render Form from Schema
        function renderForm() {{
            container.innerHTML = '';
            const t = I18N[currentLang];
            SCHEMA.questions.forEach((q, idx) => {{
                const card = document.createElement('div');
                card.className = "question-card";
                card.id = `card_${{q.id}}`;
                card.dataset.qid = q.id;

                let sampleHtml = '';
                if (q.sample_response || (q.sample_images && q.sample_images.length > 0)) {{
                    let imagesHtml = '';
                    if (q.sample_images && q.sample_images.length > 0) {{
                        imagesHtml = `
                            <div style="display:flex; gap:10px; margin-top:8px; overflow-x:auto; padding:6px 0;">
                                ${{q.sample_images.map(imgUrl => `
                                    <div style="flex-shrink:0; text-align:center;">
                                        <a href="${{imgUrl}}" target="_blank">
                                            <img src="${{imgUrl}}" style="width:140px; height:95px; object-fit:cover; border-radius:8px; border:1.5px solid #3b82f6;" alt="Sample">
                                        </a>
                                    </div>
                                `).join('')}}
                            </div>
                        `;
                    }}
                    sampleHtml = `
                        <button type="button" class="btn-sample" onclick="toggleSample('${{q.id}}')">
                            ${{q.sample_images && q.sample_images.length > 0 ? t.btn_sample_img : t.btn_sample_ans}}
                        </button>
                        <div class="sample-preview-box" id="sample_${{q.id}}">
                            ${{q.sample_response ? `<div style="font-weight:600; margin-bottom:4px;">${{q.sample_response}}</div>` : ''}}
                            ${{imagesHtml}}
                        </div>
                    `;
                }}

                const cleanTitle = q.title.replace(/^[0-9]+[.:\\s-]+/, '');
                card.innerHTML = `
                    <div class="q-header">
                        <div class="q-title-row">
                            <span class="q-title">${{idx + 1}}. ${{cleanTitle}} ${{q.required ? '<span class="q-required">*</span>' : ''}}</span>
                        </div>
                        ${{q.description ? `<p class="q-desc">${{q.description}}</p>` : ''}}
                        ${{sampleHtml}}
                    </div>
                    <div class="q-body" id="body_${{q.id}}" style="width:100%;"></div>
                    <div class="q-error" id="err_${{q.id}}"></div>
                `;

                container.appendChild(card);
                renderQuestionInput(q, card.querySelector(`#body_${{q.id}}`));
            }});

            evaluateConditionals();
        }}

        function toggleSample(qid) {{
            const el = document.getElementById(`sample_${{qid}}`);
            if (el) el.style.display = (el.style.display === 'block') ? 'none' : 'block';
        }}

        function renderQuestionInput(q, parent) {{
            const t = I18N[currentLang];
            if (q.type === 'text') {{
                const inp = document.createElement('input');
                inp.type = 'text';
                inp.className = 'q-input';
                inp.placeholder = '...';
                inp.oninput = (e) => {{ answers[q.id] = e.target.value; evaluateConditionals(); }};
                parent.appendChild(inp);
            }} else if (q.type === 'number') {{
                const inp = document.createElement('input');
                inp.type = 'number';
                inp.className = 'q-input';
                inp.placeholder = '0';
                inp.oninput = (e) => {{ answers[q.id] = e.target.value; evaluateConditionals(); }};
                parent.appendChild(inp);
            }} else if (q.type === 'dropdown') {{
                const sel = document.createElement('select');
                sel.className = 'q-input';
                sel.innerHTML = '<option value="">-- Select --</option>' + 
                    (q.options || []).map(o => `<option value="${{o}}">${{o}}</option>`).join('');
                sel.onchange = (e) => {{ answers[q.id] = e.target.value; evaluateConditionals(); }};
                parent.appendChild(sel);
            }} else if (q.type === 'single_choice') {{
                const list = document.createElement('div');
                list.className = 'choices-list';
                (q.options || []).forEach(opt => {{
                    const item = document.createElement('div');
                    item.className = 'choice-item';
                    item.textContent = opt;
                    item.onclick = () => {{
                        list.querySelectorAll('.choice-item').forEach(c => c.classList.remove('selected'));
                        item.classList.add('selected');
                        answers[q.id] = opt;
                        evaluateConditionals();
                    }};
                    list.appendChild(item);
                }});
                parent.appendChild(list);
            }} else if (q.type === 'camera_webrtc') {{
                parent.innerHTML = `
                    <div class="camera-fixed-viewport" id="camBox_${{q.id}}">
                        <!-- Camera Standby Off-Screen (Always maintains 4:3 box!) -->
                        <div class="cam-standby-placeholder" id="camOffScreen_${{q.id}}">
                            <svg viewBox="0 0 24 24" class="cam-crossed-icon"><path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.44-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z"/></svg>
                            <span class="cam-off-label" id="txtCamOff_${{q.id}}">${{t.cam_off}}</span>
                            <button type="button" class="btn-cam-activate" onclick="activateCamera('${{q.id}}')">${{t.btn_cam_on}}</button>
                        </div>
                        <video id="vid_${{q.id}}" autoplay playsinline webkit-playsinline muted></video>
                        <img id="img_${{q.id}}" class="captured-img" alt="Captured">
                        <div class="camera-overlay" id="overlay_${{q.id}}">
                            <span style="color:#fff; font-size:0.78rem; background:rgba(0,0,0,0.7); padding:5px 12px; border-radius:20px;">${{t.cam_hint}}</span>
                        </div>
                    </div>
                    <div style="display:flex; flex-direction:column; align-items:center; gap:8px; margin-top:8px;">
                        <button type="button" class="btn-camera-snap" id="snap_${{q.id}}" onclick="snapPhoto('${{q.id}}')">
                            <svg width="28" height="28" viewBox="0 0 24 24" fill="#fff"><path d="M12 9c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3m0-7C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8"/></svg>
                        </button>
                        <button type="button" class="btn-camera-retake" id="retake_${{q.id}}" onclick="retakeCam('${{q.id}}')">${{t.btn_retake}}</button>
                        <button type="button" class="btn-cam-deactivate" id="btnCamOff_${{q.id}}" onclick="deactivateCamera('${{q.id}}')">${{t.btn_cam_off}}</button>
                    </div>
                `;
            }} else if (q.type === 'signature') {{
                parent.innerHTML = `
                    <div class="sig-preview-card" onclick="openSigModal('${{q.id}}')">
                        <img id="sigImg_${{q.id}}" class="sig-preview-img" alt="Signature">
                        <div id="sigBtnLabel_${{q.id}}" style="display:flex; align-items:center; justify-content:center; gap:6px; font-weight:700; color:#0f172a !important; font-size:0.92rem;">
                            ${{t.sig_prompt}}
                        </div>
                    </div>
                `;
            }}
        }}

        // 3. Conditional Logic Branching
        function evaluateConditionals() {{
            SCHEMA.questions.forEach(q => {{
                if (q.conditional) {{
                    const parentVal = answers[q.conditional.depends_on_question_id];
                    const card = document.getElementById(`card_${{q.id}}`);
                    if (!card) return;

                    let isMatch = false;
                    if (q.conditional.operator === 'equals') {{
                        isMatch = (parentVal === q.conditional.expected_value);
                    }} else if (q.conditional.operator === 'in') {{
                        isMatch = Array.isArray(q.conditional.expected_value) && q.conditional.expected_value.includes(parentVal);
                    }}

                    if (isMatch) {{
                        card.classList.remove('hidden');
                    }} else {{
                        card.classList.add('hidden');
                        delete answers[q.id];
                    }}
                }}
            }});
        }}

        // 4. WebRTC Camera Initialization
        async function initCameraForQ(qid) {{
            const vid = document.getElementById(`vid_${{qid}}`);
            if (!vid) return;

            vid.setAttribute('playsinline', '');
            vid.setAttribute('webkit-playsinline', '');
            vid.setAttribute('muted', '');
            vid.muted = true;

            try {{
                let stream = null;
                try {{
                    stream = await navigator.mediaDevices.getUserMedia({{
                        video: {{ facingMode: {{ ideal: "environment" }}, width: {{ ideal: 1920 }}, height: {{ ideal: 1080 }} }},
                        audio: false
                    }});
                }} catch(e1) {{
                    stream = await navigator.mediaDevices.getUserMedia({{ video: true, audio: false }});
                }}
                activeVideoStream = stream;
                vid.srcObject = stream;
                await vid.play().catch(e => console.log("Play blocked:", e));
            }} catch(err) {{
                console.warn("Camera failed:", err);
            }}
        }}

        async function activateCamera(qid) {{
            const screenOff = document.getElementById(`camOffScreen_${{qid}}`);
            const vid = document.getElementById(`vid_${{qid}}`);
            const img = document.getElementById(`img_${{qid}}`);
            const overlay = document.getElementById(`overlay_${{qid}}`);
            const snapBtn = document.getElementById(`snap_${{qid}}`);
            const btnCamOff = document.getElementById(`btnCamOff_${{qid}}`);

            if (screenOff) screenOff.style.display = 'none';
            if (img) img.style.display = 'none';
            if (vid) vid.style.display = 'block';
            if (overlay) overlay.style.display = 'flex';
            if (snapBtn) snapBtn.style.display = 'flex';
            if (btnCamOff) btnCamOff.style.display = 'inline-block';

            await initCameraForQ(qid);
        }}

        function deactivateCamera(qid) {{
            if (activeVideoStream) {{
                activeVideoStream.getTracks().forEach(t => t.stop());
                activeVideoStream = null;
            }}
            const vid = document.getElementById(`vid_${{qid}}`);
            if (vid) vid.srcObject = null;
            const screenOff = document.getElementById(`camOffScreen_${{qid}}`);
            const overlay = document.getElementById(`overlay_${{qid}}`);
            const snapBtn = document.getElementById(`snap_${{qid}}`);
            const btnCamOff = document.getElementById(`btnCamOff_${{qid}}`);

            if (vid) vid.style.display = 'none';
            if (overlay) overlay.style.display = 'none';
            if (snapBtn) snapBtn.style.display = 'none';
            if (btnCamOff) btnCamOff.style.display = 'none';
            if (screenOff) screenOff.style.display = 'flex';
        }}

        function snapPhoto(qid) {{
            const vid = document.getElementById(`vid_${{qid}}`);
            const img = document.getElementById(`img_${{qid}}`);
            const overlay = document.getElementById(`overlay_${{qid}}`);
            const snapBtn = document.getElementById(`snap_${{qid}}`);
            const retakeBtn = document.getElementById(`retake_${{qid}}`);
            const btnCamOff = document.getElementById(`btnCamOff_${{qid}}`);
            const cvs = document.getElementById('hiddenCameraCanvas');

            if (!vid || !vid.videoWidth) return;
            if (navigator.vibrate) navigator.vibrate(50);

            const scale = Math.min(1.0, 1280 / Math.max(vid.videoWidth, vid.videoHeight));
            cvs.width = Math.round(vid.videoWidth * scale);
            cvs.height = Math.round(vid.videoHeight * scale);
            const ctx = cvs.getContext('2d');
            ctx.drawImage(vid, 0, 0, cvs.width, cvs.height);

            cvs.toBlob(blob => {{
                answers[qid] = blob;
                img.src = URL.createObjectURL(blob);
                img.style.display = 'block';
                vid.style.display = 'none';
                overlay.style.display = 'none';
                snapBtn.style.display = 'none';
                if (btnCamOff) btnCamOff.style.display = 'none';
                retakeBtn.style.display = 'inline-block';

                // Automatically shut off camera tracks to save battery
                if (activeVideoStream) {{
                    activeVideoStream.getTracks().forEach(t => t.stop());
                    activeVideoStream = null;
                }}
                vid.srcObject = null;
            }}, 'image/webp', 0.82);
        }}

        async function retakeCam(qid) {{
            delete answers[qid];
            const img = document.getElementById(`img_${{qid}}`);
            const retakeBtn = document.getElementById(`retake_${{qid}}`);
            const snapBtn = document.getElementById(`snap_${{qid}}`);

            if (img) img.style.display = 'none';
            if (retakeBtn) retakeBtn.style.display = 'none';

            // Auto turn camera back on
            await activateCamera(qid);

            // Shutter button renders GREY for 0.1s instead of disappearing
            if (snapBtn) {{
                snapBtn.classList.add('disabled-delay');
                setTimeout(() => {{
                    snapBtn.classList.remove('disabled-delay');
                }}, 100);
            }}
        }}

        // 5. Paper Signature Modal (Root Window Touchmove Prevention = 100% Zero Jerk)
        function preventWindowScroll(e) {{
            e.preventDefault();
        }}

        function openSigModal(qid) {{
            activeSignatureTargetQId = qid;
            modal.style.display = 'flex';
            document.body.style.overflow = 'hidden';

            // Global unconditional window touchmove cancellation: address bar physically CANNOT collapse!
            window.addEventListener('touchmove', preventWindowScroll, {{ passive: false }});

            const dpr = window.devicePixelRatio || 1;
            const r = sigCanvas.parentElement.getBoundingClientRect();
            sigCanvas.width = r.width * dpr;
            sigCanvas.height = r.height * dpr;
            sigCtx.scale(dpr, dpr);
            sigCtx.strokeStyle = "#090d16";
            sigCtx.lineWidth = 2.5;
            sigCtx.lineCap = "round";
            clearModalSig();
        }}

        function closeSigModal() {{
            modal.style.display = 'none';
            document.body.style.overflow = '';
            window.removeEventListener('touchmove', preventWindowScroll);
        }}

        function clearModalSig() {{
            sigCtx.fillStyle = "#ffffff";
            sigCtx.fillRect(0, 0, sigCanvas.width, sigCanvas.height);
            hasModalSignature = false;
        }}

        function confirmModalSig() {{
            if (!hasModalSignature) {{
                alert("Vui lòng vẽ chữ ký!");
                return;
            }}
            const dataUrl = sigCanvas.toDataURL('image/jpeg', 0.95);
            answers[activeSignatureTargetQId] = dataUrl;

            const previewImg = document.getElementById(`sigImg_${{activeSignatureTargetQId}}`);
            const label = document.getElementById(`sigBtnLabel_${{activeSignatureTargetQId}}`);
            previewImg.src = dataUrl;
            previewImg.style.display = 'block';
            label.textContent = I18N[currentLang].sig_done;
            label.style.color = "#0f172a";
            label.style.fontWeight = "700";
            closeSigModal();
        }}

        function getSigPos(e) {{
            const r = sigCanvas.getBoundingClientRect();
            const cx = e.clientX || (e.touches && e.touches[0] ? e.touches[0].clientX : 0);
            const cy = e.clientY || (e.touches && e.touches[0] ? e.touches[0].clientY : 0);
            return {{ x: cx - r.left, y: cy - r.top }};
        }}

        function startDrawing(x, y) {{
            isDrawing = true;
            hasModalSignature = true;
            sigCtx.beginPath();
            sigCtx.moveTo(x, y);
        }}

        function drawMove(x, y) {{
            if (!isDrawing) return;
            sigCtx.lineTo(x, y);
            sigCtx.stroke();
        }}

        function stopDrawing() {{
            isDrawing = false;
        }}

        sigCanvas.addEventListener('touchstart', (e) => {{
            e.preventDefault();
            e.stopPropagation();
            if (e.touches && e.touches[0]) {{
                const p = getSigPos(e.touches[0]);
                startDrawing(p.x, p.y);
            }}
        }}, {{ passive: false }});

        sigCanvas.addEventListener('touchmove', (e) => {{
            e.preventDefault();
            e.stopPropagation();
            if (e.touches && e.touches[0]) {{
                const p = getSigPos(e.touches[0]);
                drawMove(p.x, p.y);
            }}
        }}, {{ passive: false }});

        sigCanvas.addEventListener('touchend', (e) => {{
            e.preventDefault();
            e.stopPropagation();
            stopDrawing();
        }}, {{ passive: false }});

        sigCanvas.addEventListener('pointerdown', (e) => {{
            if (e.pointerType === 'touch') return;
            e.preventDefault();
            const p = getSigPos(e);
            startDrawing(p.x, p.y);
        }});

        sigCanvas.addEventListener('pointermove', (e) => {{
            if (e.pointerType === 'touch') return;
            e.preventDefault();
            const p = getSigPos(e);
            drawMove(p.x, p.y);
        }});

        sigCanvas.addEventListener('pointerup', (e) => {{
            if (e.pointerType === 'touch') return;
            stopDrawing();
        }});

        sigCanvas.addEventListener('pointercancel', (e) => {{
            if (e.pointerType === 'touch') return;
            stopDrawing();
        }});

        // 6. Reset Form
        function promptResetForm() {{ document.getElementById('confirmResetModal').style.display = 'flex'; }}
        function closeResetModal() {{ document.getElementById('confirmResetModal').style.display = 'none'; }}
        function executeResetForm() {{
            for (let k in answers) delete answers[k];
            document.querySelectorAll('#questionsContainer input, #questionsContainer select, #questionsContainer textarea').forEach(el => {{
                if (el.type === 'checkbox' || el.type === 'radio') el.checked = false;
                else el.value = '';
            }});
            document.querySelectorAll('.choice-item').forEach(c => c.classList.remove('selected'));
            deactivateCamera('camera_receipt');

            document.querySelectorAll('.sig-preview-img').forEach(img => {{ img.src = ''; img.style.display = 'none'; }});
            document.querySelectorAll('[id^="sigBtnLabel_"]').forEach(lbl => {{ lbl.textContent = I18N[currentLang].sig_prompt; }});
            hasModalSignature = false;
            if (sigCtx && sigCanvas) sigCtx.clearRect(0, 0, sigCanvas.width, sigCanvas.height);

            clientSubmissionId = 'IDEMP-' + Math.random().toString(36).substring(2, 10).toUpperCase();
            closeResetModal();
            evaluateConditionals();
            window.scrollTo({{ top: 0, behavior: 'smooth' }});
            alert(I18N[currentLang].reset_done);
        }}

        // 7. History Modal
        function openHistoryModal() {{
            document.getElementById('historyModal').style.display = 'flex';
        }}

        function closeHistoryModal() {{
            document.getElementById('historyModal').style.display = 'none';
        }}

        async function fetchHistoryAttempts() {{
            const phone = document.getElementById('historyPhoneInput').value.trim();
            const list = document.getElementById('historyResultsList');
            const t = I18N[currentLang];
            if (!phone) return;
            list.innerHTML = '<div>Loading...</div>';
            try {{
                const resp = await fetch(`/api/v1/surveys/history?phone=${{encodeURIComponent(phone)}}`);
                const data = await resp.json();
                if (!data.attempts || data.attempts.length === 0) {{
                    list.innerHTML = `<div style="padding:12px; text-align:center;">${{t.history_empty}}</div>`;
                    return;
                }}
                list.innerHTML = data.attempts.map(a => `
                    <div style="background:#1e293b; border-left:3px solid ${{a.status==='APPROVED'?'#10b981':'#ef4444'}}; padding:8px 10px; border-radius:6px;">
                        <div style="display:flex; justify-content:space-between; font-weight:700;">
                            <span>#${{a.submission_id}} (${{a.mode}})</span>
                            <span style="color:${{a.status==='APPROVED'?'#34d399':'#f87171'}};">${{a.status}}</span>
                        </div>
                        <div style="color:#cbd5e1; margin-top:2px;">${{a.reason || ''}}</div>
                        <div style="font-size:0.7rem; color:#64748b; margin-top:2px;">${{a.submitted_at || ''}}</div>
                    </div>
                `).join('');
            }} catch(e) {{
                list.innerHTML = '<div style="color:#f87171; text-align:center;">Error fetching history!</div>';
            }}
        }}

        // 8. Submit Handler
        async function handleSubmit() {{
            const t = I18N[currentLang];
            const bypassCb = document.getElementById('bypassImgToggle');
            const shouldBypass = Boolean(isTestMode && bypassCb && bypassCb.checked);

            if (!shouldBypass) {{
                if (answers['camera_receipt'] && answers['camera_receipt'].isDummy) {{
                    delete answers['camera_receipt'];
                }}
            }} else {{
                if (!answers['camera_receipt']) {{
                    const dummyCvs = document.createElement('canvas');
                    dummyCvs.width = 100; dummyCvs.height = 100;
                    const dctx = dummyCvs.getContext('2d');
                    dctx.fillStyle = '#10b981';
                    dctx.fillRect(0,0,100,100);
                    const dummyBlob = await new Promise(r => dummyCvs.toBlob(r, 'image/webp'));
                    dummyBlob.isDummy = true;
                    answers['camera_receipt'] = dummyBlob;
                }}
            }}

            for (let q of SCHEMA.questions) {{
                const val = answers[q.id];
                if (q.conditional) {{
                    const pVal = answers[q.conditional.depends_on_question_id];
                    if (q.conditional.operator === 'equals' && pVal !== q.conditional.expected_value) continue;
                }}
                if (q.required && (!val || val === "")) {{
                    alert(t.missing_required + `'${{q.title}}'`);
                    return;
                }}
            }}

            const btn = document.getElementById('btnSubmitForm');
            btn.disabled = true;
            btn.textContent = t.btn_submitting;

            const formData = new FormData();
            for (let k in answers) {{
                if (answers[k] instanceof Blob) {{
                    formData.append('survey_photo', answers[k], 'survey_camera.webp');
                }} else {{
                    formData.append(k, answers[k]);
                }}
            }}

            formData.append('is_test_mode', isTestMode ? 'true' : 'false');
            clientSubmissionId = 'IDEMP-' + Math.random().toString(36).substring(2, 10).toUpperCase();
            formData.append('idempotency_key', clientSubmissionId);
            formData.append('bypass_img_check', shouldBypass ? 'true' : 'false');

            if (answers['driver_phone']) formData.append('driver_phone', answers['driver_phone']);
            if (answers['license_plate']) formData.append('license_plate', answers['license_plate']);
            if (answers['driver_signature']) formData.append('signature_png', answers['driver_signature']);

            if (gpsCoordinates) {{
                formData.append('gps_latitude', gpsCoordinates.latitude);
                formData.append('gps_longitude', gpsCoordinates.longitude);
            }}

            try {{
                const resp = await fetch('/api/v1/survey/submit-webrtc', {{ method: 'POST', body: formData }});
                if (!resp.ok) {{
                    const errBody = await resp.text();
                    alert(`Error (${{resp.status}}): ${{errBody || 'Failed'}}`);
                    return;
                }}
                const res = await resp.json();
                if (res.status === 'APPROVED') {{
                    alert(`${{t.verdict_approved}}\\nID: #${{res.submission_id}}\\n${{res.reason || ''}}`);
                }} else {{
                    alert(`${{t.verdict_rejected}}\\nReason: ${{res.reason || 'Verification failed'}}`);
                }}
            }} catch(e) {{
                alert(t.server_err);
            }} finally {{
                btn.disabled = false;
                btn.textContent = t.btn_submit;
            }}
        }}

        window.addEventListener('DOMContentLoaded', () => {{
            applyI18n();
            renderForm();
        }});
    </script>
</body>
</html>
"""
