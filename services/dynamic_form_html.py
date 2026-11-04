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

        html, body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100dvh;
            display: flex;
            flex-direction: column;
            padding: env(safe-area-inset-top, 12px) 14px env(safe-area-inset-bottom, 24px) 14px;
            overflow-x: hidden;
            touch-action: manipulation;
            overscroll-behavior: none;
            overscroll-behavior-y: none;
            -webkit-overflow-scrolling: touch;
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

        .btn-gps:disabled {{
            opacity: 0.6;
            cursor: not-allowed;
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

        /* Viewfinder Section */
        .camera-container {{
            position: relative;
            width: 100%;
            aspect-ratio: 4 / 3;
            max-height: 40dvh;
            background: #000;
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        video {{
            width: 100%;
            height: 100%;
            object-fit: cover;
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
            display: flex;
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
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
        }}

        .btn-camera-retake {{
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: #fff;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.85rem;
            display: none;
            margin: 10px auto 0 auto;
            cursor: pointer;
            font-weight: 600;
        }}

        /* Signature Question Trigger Card */
        .sig-preview-card {{
            border: 2px dashed var(--border);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
            cursor: pointer;
            background: rgba(255,255,255,0.02);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
        }}

        .sig-preview-img {{
            max-height: 70px;
            display: none;
        }}

        /* Paper Signature Modal Popup */
        .modal-paper-overlay {{
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            width: 100vw;
            height: 100dvh;
            height: 100svh;
            height: 100vh;
            background: rgba(0, 0, 0, 0.88);
            backdrop-filter: blur(8px);
            z-index: 99999;
            align-items: center;
            justify-content: center;
            padding: 14px;
            overscroll-behavior: none;
            overscroll-behavior-y: none;
            touch-action: none;
        }}

        .paper-sheet {{
            background: #ffffff;
            color: #0f172a;
            width: 100%;
            max-width: 480px;
            border-radius: 16px;
            padding: 20px 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.8);
            border-top: 6px solid #2563eb;
        }}

        .paper-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .paper-header h3 {{
            font-size: 1.05rem;
            font-weight: 800;
            color: #0f172a;
        }}

        .paper-canvas-wrap {{
            position: relative;
            background: #f8fafc;
            border: 1.5px solid #cbd5e1;
            border-radius: 10px;
            height: 220px;
            touch-action: none;
            overflow: hidden;
        }}

        #modalSigCanvas {{
            width: 100%;
            height: 100%;
            display: block;
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

        .btn-reset-form {{
            background: transparent;
            color: #f87171;
            border: 1.5px solid rgba(239, 68, 68, 0.3);
            padding: 12px;
            border-radius: 12px;
            font-size: 0.88rem;
            font-weight: 600;
            width: 100%;
            cursor: pointer;
            margin-top: 10px;
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
    <!-- GPS Authorization Banner -->
    <div class="gps-banner" id="gpsBanner">
        <div class="gps-status-text" id="gpsStatusText">
            Cho phép truy cập vị trí để xác nhận điểm bàn giao ca
        </div>
        <button type="button" class="btn-gps" id="btnRequestGps" onclick="requestGps()">Bật vị trí</button>
    </div>

    <!-- Test Mode Banner -->
    <div class="test-banner" id="testModeBanner">
        <span>Chế độ biểu mẫu: <b id="testModeStatus">Chính thức</b></span>
        <button type="button" class="btn-test-toggle" id="btnToggleTestMode" onclick="toggleTestMode()">Bật thử nghiệm</button>
    </div>

    <header>
        <h1>{schema.title}</h1>
        <p>{schema.description or ''}</p>
    </header>

    <form id="dynamicForm" onsubmit="return false;">
        <div id="questionsContainer"></div>

        <button type="button" class="btn-submit" id="btnSubmitForm" onclick="handleSubmit()">
            <span>Gửi Báo Cáo</span>
        </button>

        <button type="button" class="btn-reset-form" onclick="promptResetForm()">
            <span>Làm lại từ đầu</span>
        </button>

        <p class="decree13-notice">
            Thông tin báo cáo ca xe được lưu trữ bảo mật nội bộ.
        </p>
    </form>

    <!-- Paper Signature Modal -->
    <div class="modal-paper-overlay" id="paperModal">
        <div class="paper-sheet">
            <div class="paper-header">
                <h3>Bảng Ký Tên Bàn Giao Ca</h3>
                <span style="font-size: 0.8rem; color: #64748b;">Dùng ngón tay ký tên</span>
            </div>
            <div class="paper-canvas-wrap">
                <canvas id="modalSigCanvas"></canvas>
                <div class="sig-baseline"></div>
                <div class="sig-baseline-text">Ký tên phía trên đường kẻ</div>
            </div>
            <div class="paper-footer">
                <button type="button" class="btn-modal cancel" onclick="closeSigModal()">Đóng</button>
                <button type="button" class="btn-modal clear" onclick="clearModalSig()">Ký lại</button>
                <button type="button" class="btn-modal confirm" onclick="confirmModalSig()">Hoàn tất chữ ký</button>
            </div>
        </div>
    </div>

    <!-- Reset Confirmation Modal -->
    <div class="confirm-modal-overlay" id="confirmResetModal">
        <div class="confirm-box">
            <h3 style="color:#ef4444; font-size:1.1rem;">Làm lại từ đầu?</h3>
            <p style="font-size:0.85rem; color:#cbd5e1;">Toàn bộ thông tin vừa nhập sẽ bị xóa để bạn nhập lại.</p>
            <div style="display:flex; gap:10px;">
                <button type="button" style="flex:1; padding:10px; border-radius:8px; border:none; background:#334155; color:#fff;" onclick="closeResetModal()">Hủy</button>
                <button type="button" style="flex:1; padding:10px; border-radius:8px; border:none; background:#ef4444; color:#fff; font-weight:700;" onclick="executeResetForm()">Đồng ý xóa</button>
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

        // Test Mode State
        let isTestMode = window.location.search.includes('test=1') || window.location.search.includes('mode=test');

        function updateTestModeUI() {{
            const banner = document.getElementById('testModeBanner');
            const statusTxt = document.getElementById('testModeStatus');
            const toggleBtn = document.getElementById('btnToggleTestMode');
            if (!banner || !statusTxt || !toggleBtn) return;

            if (isTestMode) {{
                statusTxt.textContent = "Thử nghiệm (Không lưu hệ thống)";
                banner.style.background = "rgba(245, 158, 11, 0.2)";
                banner.style.borderColor = "#f59e0b";
                toggleBtn.textContent = "Về Chính thức";
            }} else {{
                statusTxt.textContent = "Chính thức";
                banner.style.background = "rgba(30, 41, 59, 0.6)";
                banner.style.borderColor = "rgba(255, 255, 255, 0.12)";
                toggleBtn.textContent = "Bật thử nghiệm";
            }}
        }}

        function toggleTestMode() {{
            isTestMode = !isTestMode;
            updateTestModeUI();
        }}

        const container = document.getElementById('questionsContainer');
        const modal = document.getElementById('paperModal');
        const sigCanvas = document.getElementById('modalSigCanvas');
        const sigCtx = sigCanvas.getContext('2d');
        let isDrawing = false;
        let hasModalSignature = false;

        // 1. GPS Request - Fast, non-hanging location request
        function requestGps() {{
            if (!navigator.geolocation) {{
                alert("Trình duyệt không hỗ trợ định vị vị trí!");
                return;
            }}
            const btn = document.getElementById('btnRequestGps');
            const txt = document.getElementById('gpsStatusText');
            btn.disabled = true;
            btn.textContent = "Đang lấy...";
            txt.textContent = "Đang xác định vị trí...";

            navigator.geolocation.getCurrentPosition(
                (pos) => {{
                    gpsCoordinates = {{
                        latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude,
                        accuracy: pos.coords.accuracy
                    }};
                    // Close banner completely on success
                    const banner = document.getElementById('gpsBanner');
                    if (banner) banner.style.display = 'none';
                }},
                (err) => {{
                    btn.disabled = false;
                    btn.textContent = "Thử lại";
                    if (err.code === 1) {{
                        txt.textContent = "Quyền vị trí bị từ chối. Vui lòng cho phép trong cài đặt trình duyệt.";
                    }} else {{
                        txt.textContent = "Chưa thể lấy vị trí. Vui lòng bật GPS trên máy và thử lại.";
                    }}
                }},
                {{ enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 }}
            );
        }}

        // 2. Render Form from Schema
        function renderForm() {{
            container.innerHTML = '';
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
                                            <img src="${{imgUrl}}" style="width:140px; height:95px; object-fit:cover; border-radius:8px; border:1.5px solid #3b82f6;" alt="Ảnh mẫu">
                                        </a>
                                        <div style="font-size:0.7rem; color:#94a3b8; margin-top:3px;">(Bấm để xem lớn)</div>
                                    </div>
                                `).join('')}}
                            </div>
                        `;
                    }}
                    sampleHtml = `
                        <button type="button" class="btn-sample" onclick="toggleSample('${{q.id}}')">
                            ${{q.sample_images && q.sample_images.length > 0 ? 'Xem ảnh mẫu' : 'Xem câu trả lời mẫu'}}
                        </button>
                        <div class="sample-preview-box" id="sample_${{q.id}}">
                            ${{q.sample_response ? `<div style="font-weight:600; margin-bottom:4px;">${{q.sample_response}}</div>` : ''}}
                            ${{imagesHtml}}
                        </div>
                    `;
                }}

                card.innerHTML = `
                    <div class="q-header">
                        <div class="q-title-row">
                            <span class="q-title">${{idx + 1}}. ${{q.title}} ${{q.required ? '<span class="q-required">*</span>' : ''}}</span>
                        </div>
                        ${{q.description ? `<p class="q-desc">${{q.description}}</p>` : ''}}
                        ${{sampleHtml}}
                    </div>
                    <div class="q-body" id="body_${{q.id}}"></div>
                    <div class="q-error" id="err_${{q.id}}"></div>
                `;

                container.appendChild(card);
                renderQuestionInput(q, card.querySelector(`#body_${{q.id}}`));
            }});

            evaluateConditionals();
        }}

        function toggleSample(qid) {{
            const el = document.getElementById(`sample_${{qid}}`);
            if (el) {{
                el.style.display = (el.style.display === 'block') ? 'none' : 'block';
            }}
        }}

        function renderQuestionInput(q, parent) {{
            if (q.type === 'text') {{
                const inp = document.createElement('input');
                inp.type = 'text';
                inp.className = 'q-input';
                inp.placeholder = 'Nhập câu trả lời...';
                inp.oninput = (e) => {{ answers[q.id] = e.target.value; evaluateConditionals(); }};
                parent.appendChild(inp);
            }} else if (q.type === 'number') {{
                const inp = document.createElement('input');
                inp.type = 'number';
                inp.className = 'q-input';
                inp.placeholder = 'Nhập số...';
                inp.oninput = (e) => {{ answers[q.id] = e.target.value; evaluateConditionals(); }};
                parent.appendChild(inp);
            }} else if (q.type === 'dropdown') {{
                const sel = document.createElement('select');
                sel.className = 'q-input';
                sel.innerHTML = '<option value="">-- Chọn mục --</option>' + 
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
                    <div class="camera-container" id="cam_${{q.id}}">
                        <video id="vid_${{q.id}}" autoplay playsinline webkit-playsinline muted></video>
                        <img id="img_${{q.id}}" class="captured-img" alt="Captured">
                        <div class="camera-overlay" id="overlay_${{q.id}}">
                            <span style="color:#fff; font-size:0.78rem; background:rgba(0,0,0,0.7); padding:5px 12px; border-radius:20px;">Căn chỉnh đồng hồ xe hoặc hóa đơn vào khung</span>
                        </div>
                    </div>
                    <div id="camErr_${{q.id}}" style="display:none; text-align:center; padding:10px; font-size:0.8rem; color:#f87171;">
                        Không thể mở camera tự động. Vui lòng bấm nút chụp bên dưới:
                    </div>
                    <div style="display:flex; flex-direction:column; align-items:center; gap:8px; margin-top:10px;">
                        <button type="button" class="btn-camera-snap" id="snap_${{q.id}}" onclick="snapPhoto('${{q.id}}')">
                            <svg width="28" height="28" viewBox="0 0 24 24" fill="#fff"><path d="M12 9c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3m0-7C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8"/></svg>
                        </button>
                        <button type="button" class="btn-camera-retake" id="retake_${{q.id}}" onclick="retakeCam('${{q.id}}')">Chụp lại</button>
                        <input type="file" id="fileFallback_${{q.id}}" accept="image/*" capture="environment" style="display:none;" onchange="handleFallbackCapture(this, '${{q.id}}')">
                        <button type="button" id="btnFallback_${{q.id}}" style="display:none; background:#334155; color:#fff; border:none; padding:8px 16px; border-radius:8px; font-size:0.82rem; font-weight:600;" onclick="document.getElementById('fileFallback_${{q.id}}').click()">Chụp ảnh qua ứng dụng máy ảnh</button>
                    </div>
                `;
                initCameraForQ(q.id);
            }} else if (q.type === 'signature') {{
                parent.innerHTML = `
                    <div class="sig-preview-card" onclick="openSigModal('${{q.id}}')">
                        <img id="sigImg_${{q.id}}" class="sig-preview-img" alt="Signature">
                        <div id="sigBtnLabel_${{q.id}}" style="display:flex; align-items:center; gap:6px; font-weight:700; color:#60a5fa;">
                            Bấm vào đây để ký tên trên giấy
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
                        const wasHidden = card.classList.contains('hidden');
                        card.classList.remove('hidden');
                        if (wasHidden && q.type === 'camera_webrtc') {{
                            initCameraForQ(q.id);
                        }}
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
                await vid.play().catch(e => console.log("Auto-play blocked:", e));
            }} catch(err) {{
                console.warn("Hardware camera unavailable:", err);
                const errBox = document.getElementById(`camErr_${{qid}}`);
                const fallbackBtn = document.getElementById(`btnFallback_${{qid}}`);
                const snapBtn = document.getElementById(`snap_${{qid}}`);
                if (errBox) errBox.style.display = 'block';
                if (fallbackBtn) fallbackBtn.style.display = 'inline-block';
                if (snapBtn) snapBtn.style.display = 'none';
            }}
        }}

        function handleFallbackCapture(input, qid) {{
            if (input.files && input.files[0]) {{
                const file = input.files[0];
                const img = document.getElementById(`img_${{qid}}`);
                const vid = document.getElementById(`vid_${{qid}}`);
                const overlay = document.getElementById(`overlay_${{qid}}`);
                const retakeBtn = document.getElementById(`retake_${{qid}}`);
                const fallbackBtn = document.getElementById(`btnFallback_${{qid}}`);

                answers[qid] = file;
                img.src = URL.createObjectURL(file);
                img.style.display = 'block';
                if (vid) vid.style.display = 'none';
                if (overlay) overlay.style.display = 'none';
                if (retakeBtn) retakeBtn.style.display = 'inline-block';
                if (fallbackBtn) fallbackBtn.style.display = 'none';
            }}
        }}

        function snapPhoto(qid) {{
            const vid = document.getElementById(`vid_${{qid}}`);
            const img = document.getElementById(`img_${{qid}}`);
            const overlay = document.getElementById(`overlay_${{qid}}`);
            const snapBtn = document.getElementById(`snap_${{qid}}`);
            const retakeBtn = document.getElementById(`retake_${{qid}}`);
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
                retakeBtn.style.display = 'block';
            }}, 'image/webp', 0.82);
        }}

        function retakeCam(qid) {{
            delete answers[qid];
            const img = document.getElementById(`img_${{qid}}`);
            const vid = document.getElementById(`vid_${{qid}}`);
            const overlay = document.getElementById(`overlay_${{qid}}`);
            const snapBtn = document.getElementById(`snap_${{qid}}`);
            const retakeBtn = document.getElementById(`retake_${{qid}}`);
            const fallbackBtn = document.getElementById(`btnFallback_${{qid}}`);

            img.style.display = 'none';
            if (activeVideoStream) {{
                vid.style.display = 'block';
                overlay.style.display = 'flex';
                snapBtn.style.display = 'flex';
                retakeBtn.style.display = 'none';
            }} else {{
                fallbackBtn.style.display = 'inline-block';
                retakeBtn.style.display = 'none';
            }}
        }}

        // 5. Paper Signature Modal with Foolproof Scroll Lock
        let savedScrollY = 0;

        function openSigModal(qid) {{
            activeSignatureTargetQId = qid;
            savedScrollY = window.pageYOffset || document.documentElement.scrollTop;

            document.body.style.position = 'fixed';
            document.body.style.top = `-${{savedScrollY}}px`;
            document.body.style.width = '100%';
            document.body.style.overflow = 'hidden';
            document.body.style.touchAction = 'none';

            modal.style.display = 'flex';

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
            document.body.style.position = '';
            document.body.style.top = '';
            document.body.style.width = '';
            document.body.style.overflow = '';
            document.body.style.touchAction = '';
            window.scrollTo(0, savedScrollY);
        }}

        modal.addEventListener('touchmove', (e) => {{
            if (e.target !== sigCanvas) {{
                e.preventDefault();
                e.stopPropagation();
            }}
        }}, {{ passive: false }});

        function clearModalSig() {{
            sigCtx.clearRect(0, 0, sigCanvas.width, sigCanvas.height);
            hasModalSignature = false;
        }}

        function confirmModalSig() {{
            if (!hasModalSignature) {{
                alert("Vui lòng dùng ngón tay vẽ chữ ký trước khi bấm Hoàn tất!");
                return;
            }}
            const dataUrl = sigCanvas.toDataURL('image/png');
            answers[activeSignatureTargetQId] = dataUrl;

            const previewImg = document.getElementById(`sigImg_${{activeSignatureTargetQId}}`);
            const label = document.getElementById(`sigBtnLabel_${{activeSignatureTargetQId}}`);
            previewImg.src = dataUrl;
            previewImg.style.display = 'block';
            label.textContent = "Chữ ký đã ghi nhận (Bấm để ký lại)";
            closeSigModal();
        }}

        function getSigPos(e) {{
            const r = sigCanvas.getBoundingClientRect();
            const cx = e.touches ? e.touches[0].clientX : e.clientX;
            const cy = e.touches ? e.touches[0].clientY : e.clientY;
            return {{ x: cx - r.left, y: cy - r.top }};
        }}

        sigCanvas.addEventListener('mousedown', (e) => {{ isDrawing = true; hasModalSignature = true; const p = getSigPos(e); sigCtx.beginPath(); sigCtx.moveTo(p.x, p.y); }});
        sigCanvas.addEventListener('mousemove', (e) => {{ if (!isDrawing) return; const p = getSigPos(e); sigCtx.lineTo(p.x, p.y); sigCtx.stroke(); }});
        window.addEventListener('mouseup', () => isDrawing = false);

        sigCanvas.addEventListener('touchstart', (e) => {{ e.preventDefault(); e.stopPropagation(); isDrawing = true; hasModalSignature = true; const p = getSigPos(e); sigCtx.beginPath(); sigCtx.moveTo(p.x, p.y); }}, {{ passive: false }});
        sigCanvas.addEventListener('touchmove', (e) => {{ e.preventDefault(); e.stopPropagation(); if (!isDrawing) return; const p = getSigPos(e); sigCtx.lineTo(p.x, p.y); sigCtx.stroke(); }}, {{ passive: false }});
        window.addEventListener('touchend', () => isDrawing = false);

        // 6. Reset Form with Confirmation
        function promptResetForm() {{ document.getElementById('confirmResetModal').style.display = 'flex'; }}
        function closeResetModal() {{ document.getElementById('confirmResetModal').style.display = 'none'; }}
        function executeResetForm() {{
            for (let k in answers) delete answers[k];
            closeResetModal();
            renderForm();
            alert("Biểu mẫu đã được làm lại từ đầu!");
        }}

        // 7. Submit Handler
        async function handleSubmit() {{
            for (let q of SCHEMA.questions) {{
                const val = answers[q.id];
                if (q.conditional) {{
                    const pVal = answers[q.conditional.depends_on_question_id];
                    if (q.conditional.operator === 'equals' && pVal !== q.conditional.expected_value) continue;
                }}
                if (q.required && (!val || val === "")) {{
                    alert(`Vui lòng trả lời câu hỏi bắt buộc: '${{q.title}}'`);
                    return;
                }}
                if (q.validation && q.validation.regex_pattern && val) {{
                    const reg = new RegExp(q.validation.regex_pattern);
                    if (!reg.test(String(val))) {{
                        alert(q.validation.regex_error_message || `Dữ liệu '${{q.title}}' không đúng định dạng!`);
                        return;
                    }}
                }}
            }}

            const btn = document.getElementById('btnSubmitForm');
            btn.disabled = true;
            btn.textContent = "Đang gửi báo cáo...";

            const formData = new FormData();
            for (let k in answers) {{
                if (answers[k] instanceof Blob) {{
                    formData.append('survey_photo', answers[k], 'survey_camera.webp');
                }} else {{
                    formData.append(k, answers[k]);
                }}
            }}

            formData.append('is_test_mode', isTestMode ? 'true' : 'false');
            if (answers['driver_phone']) formData.append('driver_phone', answers['driver_phone']);
            if (answers['license_plate']) formData.append('license_plate', answers['license_plate']);
            if (answers['driver_signature']) formData.append('signature_png', answers['driver_signature']);

            if (gpsCoordinates) {{
                formData.append('gps_latitude', gpsCoordinates.latitude);
                formData.append('gps_longitude', gpsCoordinates.longitude);
            }}

            try {{
                const resp = await fetch('/api/v1/survey/submit-webrtc', {{ method: 'POST', body: formData }});
                const res = await resp.json();
                if (res.status === 'APPROVED') {{
                    alert(`BÁO CÁO HỢP LỆ!\\nMã phiếu: #${{res.submission_id}}\\n${{res.reason || ''}}`);
                }} else {{
                    alert(`CẦN KIỂM TRA LẠI!\\nLý do: ${{res.reason || 'Dữ liệu không khớp'}}\\nVui lòng kiểm tra lại.`);
                }}
            }} catch(e) {{
                alert("Lỗi kết nối máy chủ! Vui lòng thử lại.");
            }} finally {{
                btn.disabled = false;
                btn.textContent = "Gửi Báo Cáo";
            }}
        }}

        window.addEventListener('DOMContentLoaded', () => {{
            renderForm();
            updateTestModeUI();
        }});
    </script>
</body>
</html>
"""
