import html
import json
import math
import os
from typing import Dict, Optional, List, Any
from services.form_engine import FormSchema

DEFAULT_SAMPLE_IMAGES: List[Dict[str, Any]] = [
    {
        "filename": "sample_app_profile.jpg",
        "title": "Ảnh Mẫu 1: Màn hình hồ sơ ứng dụng (App Profile)",
        "slot_badge": "Slot 1: Hồ Sơ App",
        "dimensions": "1568 × 1568",
        "size_kb": "2.1 MB",
        "desc": "Ảnh chụp màn hình menu/tiện ích ứng dụng tài xế GSM: Ví tài xế, Chương trình thưởng, Tiện ích bổ trợ, Kiểm tra an toàn vận doanh.",
    },
    {
        "filename": "sample_app_income.jpg",
        "title": "Ảnh Mẫu 2: Màn hình thống kê thu nhập (App Income)",
        "slot_badge": "Slot 2: Thu Nhập App",
        "dimensions": "1568 × 1568",
        "size_kb": "2.5 MB",
        "desc": "Ảnh chụp màn hình thống kê thu nhập thực tế: Tổng thu nhập 2.721.000 đ, Doanh số 2.664.000 đ, Chiết khấu -749.409 đ, Thu nhập ròng 1.971.591 đ, 18 chuyến.",
    },
]

def get_sample_images_list() -> List[Dict[str, Any]]:
    samples_dir = os.path.join(os.getcwd(), "data", "test_samples")
    result = []

    for item in DEFAULT_SAMPLE_IMAGES:
        fname = item["filename"]
        fpath = os.path.join(samples_dir, fname)
        entry = dict(item)
        if os.path.isfile(fpath):
            sz = os.path.getsize(fpath)
            entry["size_kb"] = f"{sz / 1024:.1f} KB" if sz < 1024 * 1024 else f"{sz / (1024 * 1024):.1f} MB"
        result.append(entry)

    return result
def render_forms_list_html(forms_registry: Dict[str, FormSchema], page: int = 1, page_size: int = 6) -> str:
    # Deduplicate forms by form_id to prevent phantom duplicates
    unique_forms: Dict[str, FormSchema] = {}
    for f in forms_registry.values():
        unique_forms[f.form_id] = f
    
    all_forms = list(unique_forms.values())
    total_forms = len(all_forms)
    total_pages = max(1, math.ceil(total_forms / page_size))
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * page_size
    paged_forms = all_forms[start_idx : start_idx + page_size]

    forms_cards_html = ""
    for f in paged_forms:
        is_test = bool(f.settings and f.settings.is_test_mode)
        badge_cls = "badge test" if is_test else "badge active"
        badge_txt = "Thử nghiệm" if is_test else "Chính thức"
        form_link = f"/forms/{f.form_id}?mode=test" if is_test else f"/forms/{f.form_id}"
        forms_cards_html += f"""
        <div class="form-item-card">
            <h3>{html.escape(f.title)}</h3>
            <div class="form-item-meta">
                <div>Mã ID: <code>{html.escape(f.form_id)}</code></div>
                <div>Số câu hỏi: <b>{len(f.questions)}</b></div>
                <div>Chế độ: <span class="{badge_cls}">{badge_txt}</span></div>
                <div>Quyền truy cập: <b>{f.settings.access_control if f.settings else 'public'}</b></div>
            </div>
            <div class="form-actions">
                <a href="{form_link}" target="_blank" class="btn-action view">Mở Form</a>
                <a href="/admin/forms/edit/{f.form_id}" class="btn-action edit">Sửa</a>
            </div>
        </div>
        """
    sample_images = get_sample_images_list()
    samples_cards_html = ""
    for s in sample_images:
        fname = s["filename"]
        title_esc = html.escape(s["title"])
        badge_esc = html.escape(s["slot_badge"])
        dim_esc = html.escape(s["dimensions"])
        size_esc = html.escape(s["size_kb"])
        desc_esc = html.escape(s["desc"])
        url = f"/samples/{fname}"

        samples_cards_html += f"""
        <div class="sample-card">
            <div class="sample-thumb-wrap" onclick="window.open('{url}', '_blank')">
                <img src="{url}" alt="{title_esc}" loading="lazy">
                <span class="sample-thumb-badge">{badge_esc}</span>
                <span class="sample-thumb-meta">{dim_esc} • {size_esc}</span>
            </div>
            <div class="sample-body">
                <div class="sample-title">{title_esc}</div>
                <div class="sample-desc">{desc_esc}</div>
                <div class="sample-actions">
                    <a href="{url}" download="{fname}" class="btn-download-sample">
                        <svg viewBox="0 0 24 24" style="width:14px; height:14px; fill:currentColor;"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/></svg>
                        Tải Về
                    </a>
                    <button type="button" onclick="copySampleUrl('{url}', this)" class="btn-copy-sample">
                        <svg viewBox="0 0 24 24" style="width:14px; height:14px; fill:currentColor;"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
                        Copy Link
                    </button>
                </div>
            </div>
        </div>
        """

    prev_disabled = "disabled" if current_page <= 1 else ""
    next_disabled = "disabled" if current_page >= total_pages else ""
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Trust Bridge - Danh Sách Biểu Mẫu</title>
    <style>
        :root {{
            --bg: #090d16;
            --surface: #131b2e;
            --border: #23314f;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --success: #10b981;
            --text: #f8fafc;
            --muted: #94a3b8;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px 14px;
        }}

        .container {{ max-width: 900px; margin: 0 auto; }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
            gap: 12px;
            flex-wrap: wrap;
        }}

        header h1 {{ font-size: 1.3rem; font-weight: 800; }}
        header p {{ font-size: 0.82rem; color: var(--muted); margin-top: 2px; }}

        .btn-create {{
            background: var(--accent);
            color: #fff;
            padding: 10px 16px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 700;
            font-size: 0.85rem;
            display: inline-block;
        }}

        .form-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 14px;
            margin-bottom: 24px;
        }}

        @media (max-width: 640px) {{
            .form-grid {{ grid-template-columns: 1fr; }}
        }}

        .form-item-card {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}

        .form-item-card h3 {{ font-size: 1rem; color: #fff; line-height: 1.3; }}

        .form-item-meta {{
            font-size: 0.8rem;
            color: var(--muted);
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.72rem;
            font-weight: 700;
        }}
        .badge.active {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
        .badge.test {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; }}

        .form-actions {{ display: flex; gap: 8px; margin-top: 6px; }}

        .btn-action {{
            flex: 1;
            text-align: center;
            padding: 8px 12px;
            font-size: 0.82rem;
            font-weight: 700;
            border-radius: 6px;
            text-decoration: none;
        }}
        .btn-action.view {{ background: #2563eb; color: #fff; }}
        .btn-action.edit {{ background: #334155; color: #e2e8f0; }}

        /* Pagination Controls */
        .pagination {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
            padding: 16px 0;
        }}

        .btn-page {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            color: #fff;
            padding: 8px 16px;
            border-radius: 8px;
            text-decoration: none;
            font-size: 0.85rem;
            font-weight: 600;
        }}

        .btn-page.disabled {{
            opacity: 0.4;
            pointer-events: none;
        }}

        .page-indicator {{ font-size: 0.85rem; color: var(--muted); }}

        .btn-samples-nav {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid var(--border);
            color: #38bdf8;
            padding: 9px 14px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 700;
            font-size: 0.85rem;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-samples-nav:hover {{
            border-color: #38bdf8;
            background: rgba(56, 189, 248, 0.1);
        }}
        .samples-section {{
            margin-top: 36px;
            padding-top: 24px;
            border-top: 1px solid var(--border);
        }}
        .samples-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .samples-header h2 {{
            font-size: 1.15rem;
            font-weight: 800;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .samples-header p {{
            font-size: 0.82rem;
            color: var(--muted);
            margin-top: 4px;
        }}
        .samples-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 14px;
        }}
        .sample-card {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.15s ease, border-color 0.15s ease;
        }}
        .sample-card:hover {{
            border-color: rgba(37, 99, 235, 0.6);
        }}
        .sample-thumb-wrap {{
            position: relative;
            width: 100%;
            height: 180px;
            background: #060911;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
        }}
        .sample-thumb-wrap img {{
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            transition: transform 0.2s ease;
        }}
        .sample-thumb-wrap:hover img {{
            transform: scale(1.04);
        }}
        .sample-thumb-badge {{
            position: absolute;
            top: 8px;
            left: 8px;
            background: rgba(9, 13, 22, 0.85);
            backdrop-filter: blur(4px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #38bdf8;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
        }}
        .sample-thumb-meta {{
            position: absolute;
            bottom: 8px;
            right: 8px;
            background: rgba(9, 13, 22, 0.85);
            backdrop-filter: blur(4px);
            color: var(--muted);
            font-size: 0.7rem;
            font-weight: 600;
            padding: 2px 6px;
            border-radius: 4px;
        }}
        .sample-body {{
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex: 1;
        }}
        .sample-title {{
            font-size: 0.95rem;
            font-weight: 700;
            color: #fff;
            line-height: 1.3;
        }}
        .sample-desc {{
            font-size: 0.8rem;
            color: var(--muted);
            line-height: 1.4;
            flex: 1;
        }}
        .sample-actions {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 6px;
        }}
        .btn-download-sample {{
            background: var(--accent);
            color: #fff;
            text-align: center;
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 700;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
        }}
        .btn-download-sample:hover {{
            background: var(--accent-hover);
        }}
        .btn-copy-sample {{
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border);
            color: var(--text);
            text-align: center;
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
        }}
        .btn-copy-sample:hover {{
            background: rgba(255, 255, 255, 0.12);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <button type="button" onclick="toggleAdminLang()" style="display:inline-flex; align-items:center; gap:6px; background:rgba(30, 41, 59, 0.85); border:1px solid var(--border); color:#f8fafc; padding:6px 14px; border-radius:20px; font-size:0.82rem; font-weight:700; cursor:pointer;">
                <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:#60a5fa;"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
                <span id="adminLangCode">VI</span>
            </button>
        </div>
        <header>
            <div>
                <h1 id="txtAdminTitle">Danh Sách Biểu Mẫu Khảo Sát</h1>
                <p id="txtAdminSub">Tổng cộng: {total_forms} biểu mẫu trong hệ thống</p>
            </div>
            <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                <a href="/admin/submissions" class="btn-samples-nav" id="btnAdminSubmissions">📊 Dữ Liệu Nộp</a>
                <a href="#test-samples-section" class="btn-samples-nav" id="btnAdminSamples">🖼️ Ảnh Mẫu Test</a>
                <a href="/admin/forms/create" class="btn-create" id="btnAdminCreate">+ Tạo Biểu Mẫu Mới</a>
            </div>
        </header>

        <div class="form-grid">
            {forms_cards_html}
        </div>

        <div class="pagination">
            <a href="/admin/forms?page={current_page - 1}" class="btn-page {prev_disabled}">Trang trước</a>
            <span class="page-indicator">Trang {current_page} / {total_pages}</span>
            <a href="/admin/forms?page={current_page + 1}" class="btn-page {next_disabled}">Trang sau</a>
        </div>

        <section id="test-samples-section" class="samples-section">
            <div class="samples-header">
                <div>
                    <h2 id="txtSamplesTitle">🖼️ Kho Ảnh Mẫu Thử Nghiệm</h2>
                    <p id="txtSamplesSub">Tải về máy điện thoại hoặc copy link ảnh chuẩn để kiểm thử các slot camera WebRTC & AI</p>
                </div>
                <span class="badge active">{len(sample_images)} Ảnh Có Sẵn</span>
            </div>
            <div class="samples-grid">
                {samples_cards_html}
            </div>
        </section>
    </div>
    <script>
        let currentLang = localStorage.getItem('tb_lang') || 'vi';
        function copySampleUrl(path, btn) {{
            const fullUrl = window.location.origin + path;
            navigator.clipboard.writeText(fullUrl).then(() => {{
                const orig = btn.innerHTML;
                btn.innerHTML = "✅ Đã copy!";
                setTimeout(() => {{ btn.innerHTML = orig; }}, 2000);
            }}).catch(() => {{
                prompt("Sao chép link ảnh:", fullUrl);
            }});
        }}
        function applyAdminI18n() {{
            document.getElementById('adminLangCode').textContent = currentLang.toUpperCase();
            const btnSamples = document.getElementById('btnAdminSamples');
            const txtSTitle = document.getElementById('txtSamplesTitle');
            const txtSSub = document.getElementById('txtSamplesSub');
            if (currentLang === 'en') {{
                document.getElementById('txtAdminTitle').textContent = "Survey Forms Management";
                document.getElementById('txtAdminSub').textContent = "Total: {total_forms} active forms in system";
                document.getElementById('btnAdminCreate').textContent = "+ Create New Form";
                if (btnSamples) btnSamples.textContent = "🖼️ Test Samples";
                if (txtSTitle) txtSTitle.textContent = "🖼️ Test Sample Images Gallery";
                if (txtSSub) txtSSub.textContent = "Download to phone camera roll or copy links to test WebRTC camera slots & AI verification";
            }} else {{
                document.getElementById('txtAdminTitle').textContent = "Danh Sách Biểu Mẫu Khảo Sát";
                document.getElementById('txtAdminSub').textContent = "Tổng cộng: {total_forms} biểu mẫu trong hệ thống";
                document.getElementById('btnAdminCreate').textContent = "+ Tạo Biểu Mẫu Mới";
                if (btnSamples) btnSamples.textContent = "🖼️ Ảnh Mẫu Test";
                if (txtSTitle) txtSTitle.textContent = "🖼️ Kho Ảnh Mẫu Thử Nghiệm";
                if (txtSSub) txtSSub.textContent = "Tải về máy điện thoại hoặc copy link ảnh chuẩn để kiểm thử các slot camera WebRTC & AI";
            }}
        }}
        function toggleAdminLang() {{
            currentLang = currentLang === 'vi' ? 'en' : 'vi';
            localStorage.setItem('tb_lang', currentLang);
            applyAdminI18n();
        }}
        window.addEventListener('DOMContentLoaded', applyAdminI18n);
    </script>
</body>
</html>
"""

def render_form_editor_html(schema: Optional[FormSchema] = None, is_new: bool = False) -> str:
    schema_dict = schema.model_dump() if schema else {
        "form_id": "",
        "title": "",
        "description": "",
        "settings": {
            "response_limit": 0,
            "access_control": "public",
            "allow_anonymous": False,
            "allow_edit_after_submit": False,
            "is_test_mode": False,
            "notify_email": "",
            "notify_telegram_chat_id": None,
            "image_checks": {
                "enable_blur_check": True,
                "blur_threshold": 40.0,
                "enable_ai_vision": True,
                "enable_vehicle_match": True,
                "enable_anti_fraud": True,
                "enable_ai_generated_check": True,
                "enable_screen_photo_check": True
            }
        },
        "questions": []
    }

    schema_json_str = json.dumps(schema_dict, ensure_ascii=False)
    title_val = html.escape(schema.title) if schema else ''
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>{'Tạo Biểu Mẫu Mới' if is_new else 'Chỉnh Sửa Biểu Mẫu: ' + title_val}</title>
    <style>
        :root {{
            --bg: #090d16;
            --surface: #131b2e;
            --surface-hover: #1c2742;
            --border: #23314f;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --success: #10b981;
            --danger: #ef4444;
            --text: #f8fafc;
            --muted: #94a3b8;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 16px 14px 40px 14px;
        }}

        .container {{ max-width: 860px; margin: 0 auto; }}


        /* 3 Clean Tabs Navigation */
        .tab-nav {{
            display: flex;
            gap: 6px;
            margin-bottom: 16px;
            border-bottom: 1.5px solid var(--border);
            padding-bottom: 2px;
            overflow-x: auto;
        }}

        .tab-btn {{
            background: transparent;
            border: none;
            color: var(--muted);
            padding: 10px 16px;
            font-size: 0.88rem;
            font-weight: 700;
            cursor: pointer;
            border-bottom: 2.5px solid transparent;
            transition: all 0.2s;
            white-space: nowrap;
        }}

        .tab-btn.active {{
            color: #60a5fa;
            border-bottom-color: var(--accent);
        }}

        .tab-panel {{
            display: none;
            animation: fadeIn 0.2s ease;
        }}
        .tab-panel.active {{ display: flex; flex-direction: column; gap: 14px; }}

        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(4px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}

        /* Responsive Form Layout */
        .card {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 14px;
            padding: 18px 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        .field-group {{ display: flex; flex-direction: column; gap: 6px; }}
        .field-group label {{ font-size: 0.8rem; font-weight: 600; color: var(--muted); }}

        .field-group input, .field-group select, .field-group textarea {{
            background: var(--bg);
            border: 1.5px solid var(--border);
            color: #fff;
            padding: 12px 14px;
            border-radius: 10px;
            font-size: 0.92rem;
            outline: none;
            width: 100%;
        }}

        .field-group input:focus, .field-group select:focus, .field-group textarea:focus {{
            border-color: var(--accent);
        }}

        /* Mobile Responsive Grid Override */
        .row-2, .row-3 {{
            display: grid;
            gap: 12px;
        }}
        .row-2 {{ grid-template-columns: 1fr 1fr; }}
        .row-3 {{ grid-template-columns: 1fr 1fr 1fr; }}

        /* STACK VERTICALLY ON MOBILE SO DROPDOWNS ARE NEVER SQUISHED */
        @media (max-width: 640px) {{
            .row-2, .row-3 {{
                grid-template-columns: 1fr !important;
            }}
        }}

        /* Question Item Card */
        .question-item {{
            background: var(--bg);
            border: 1.5px solid var(--border);
            border-radius: 10px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}

        .btn-delete {{
            background: rgba(239, 68, 68, 0.2);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.4);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            cursor: pointer;
            font-weight: 700;
        }}

        .btn-add-q {{
            background: #1e293b;
            color: #60a5fa;
            border: 1.5px dashed var(--accent);
            padding: 12px;
            border-radius: 10px;
            font-weight: 700;
            font-size: 0.88rem;
            cursor: pointer;
            width: 100%;
        }}

        .btn-save-master {{
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            color: #fff;
            border: none;
            padding: 16px;
            border-radius: 12px;
            font-size: 1rem;
            font-weight: 800;
            cursor: pointer;
            margin-top: 16px;
            width: 100%;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <button type="button" onclick="toggleEditorLang()" style="display:inline-flex; align-items:center; gap:6px; background:rgba(30, 41, 59, 0.85); border:1px solid var(--border); color:#f8fafc; padding:6px 14px; border-radius:20px; font-size:0.82rem; font-weight:700; cursor:pointer;">
                <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:#60a5fa;"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
                <span id="editorLangCode">VI</span>
            </button>
            <a href="/admin/forms" class="back-link" id="linkBackAdmin">Quay lại danh sách biểu mẫu</a>
        </div>
        <header>
            <h1 id="txtEditorTitle">{'Tạo Biểu Mẫu Mới' if is_new else 'Chỉnh Sửa: ' + (html.escape(schema.title) if schema else '')}</h1>
        </header>

        <!-- 3 Tabs Navigation -->
        <div class="tab-nav">
            <button type="button" class="tab-btn active" id="btnTabCommon" onclick="switchTab('common')">1. Thông tin chung</button>
            <button type="button" class="tab-btn" id="btnTabOptions" onclick="switchTab('options')">2. Tùy chọn & Cài đặt</button>
            <button type="button" class="tab-btn" id="btnTabQuestions" onclick="switchTab('questions')">3. Danh sách câu hỏi</button>
        </div>

        <form id="editorForm" onsubmit="return false;">
            <!-- Tab 1: Common Info -->
            <div class="tab-panel active" id="tabCommon">
                <div class="card">
                    <div class="row-2">
                        <div class="field-group">
                            <label for="formId">Mã định danh (Form ID - viết liền không dấu) *</label>
                            <input type="text" id="formId" value="{html.escape(schema_dict['form_id'])}" {'readonly' if not is_new else ''} required>
                        </div>
                        <div class="field-group">
                            <label for="formTitle">Tên biểu mẫu (Tiêu đề) *</label>
                            <input type="text" id="formTitle" value="{html.escape(schema_dict['title'])}" required>
                        </div>
                    </div>
                    <div class="field-group">
                        <label for="formDesc">Mô tả biểu mẫu</label>
                        <textarea id="formDesc" rows="3" placeholder="Ghi chú hướng dẫn cho tài xế...">{html.escape(schema_dict.get('description') or '')}</textarea>
                    </div>
                </div>
            </div>

            <!-- Tab 2: Options & Settings -->
            <div class="tab-panel" id="tabOptions">
                <div class="card">
                    <div class="row-2">
                        <div class="field-group">
                            <label for="responseLimit">Giới hạn số lượng phản hồi (0 = Không giới hạn)</label>
                            <input type="number" id="responseLimit" value="{schema_dict['settings'].get('response_limit') or 0}">
                        </div>
                        <div class="field-group">
                            <label for="accessControl">Quyền truy cập</label>
                            <select id="accessControl">
                                <option value="public" {'selected' if schema_dict['settings'].get('access_control')=='public' else ''}>Công khai (Bất kỳ ai có link)</option>
                                <option value="private" {'selected' if schema_dict['settings'].get('access_control')=='private' else ''}>Nội bộ (Chỉ tài xế được duyệt)</option>
                            </select>
                        </div>
                    </div>

                    <div class="row-2">
                        <div class="field-group">
                            <label for="isTestMode">Chế độ vận hành</label>
                            <select id="isTestMode">
                                <option value="false" {'selected' if not schema_dict['settings'].get('is_test_mode') else ''}>Chính thức (Lưu PostgreSQL SSOT)</option>
                                <option value="true" {'selected' if schema_dict['settings'].get('is_test_mode') else ''}>Thử nghiệm (Lưu Dev Storage tạm)</option>
                            </select>
                        </div>
                        <div class="field-group">
                            <label for="notifyChatId">Telegram Chat ID nhận thông báo</label>
                            <input type="number" id="notifyChatId" value="{schema_dict['settings'].get('notify_telegram_chat_id') or ''}" placeholder="Ví dụ: -1001234567890">
                        </div>
                    </div>

                    <div style="margin-top: 14px; border-top: 1px solid var(--border); padding-top: 14px;">
                        <h3 style="font-size: 0.95rem; font-weight: 700; color: #60a5fa; margin-bottom: 10px;">Cấu hình kiểm tra ảnh toàn cục (Global Image Checks)</h3>
                        <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; margin-bottom: 12px;">
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                                <input type="checkbox" id="globalEnableBlurCheck" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_blur_check', True) else ''}>
                                Kiểm tra độ mờ / rung tay
                            </label>
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                                <input type="checkbox" id="globalEnableAiVision" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_ai_vision', True) else ''}>
                                Kiểm tra AI Vision trích xuất hóa đơn
                            </label>
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                                <input type="checkbox" id="globalEnableVehicleMatch" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_vehicle_match', True) else ''}>
                                Kiểm tra đối chiếu biển số phương tiện
                            </label>
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                                <input type="checkbox" id="globalEnableAntiFraud" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_anti_fraud', True) else ''}>
                                Kiểm tra chống gian lận & ảnh trùng lặp
                            </label>
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                                <input type="checkbox" id="globalEnableAiGeneratedCheck" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_ai_generated_check', True) else ''}>
                                Kiểm tra ảnh AI tổng hợp
                            </label>
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                                <input type="checkbox" id="globalEnableScreenPhotoCheck" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_screen_photo_check', False) else ''}>
                                Kiểm tra ảnh chụp màn hình
                            </label>
                        </div>
                        <div class="field-group" style="max-width: 320px;">
                            <label for="globalBlurThreshold">Ngưỡng độ mờ tùy chỉnh (mặc định 40)</label>
                            <input type="number" step="0.1" id="globalBlurThreshold" value="{(schema_dict['settings'].get('image_checks') or {}).get('blur_threshold') if (schema_dict['settings'].get('image_checks') or {}).get('blur_threshold') is not None else 40}" placeholder="40">
                        </div>
                    </div>
                </div>
            </div>

            <!-- Tab 3: Questions -->
            <div class="tab-panel" id="tabQuestions">
                <div class="card">
                    <div id="questionsBuilderList" style="display:flex; flex-direction:column; gap:12px;"></div>
                    <button type="button" class="btn-add-q" onclick="addQuestion()">+ Thêm Câu Hỏi Mới</button>
                </div>
            </div>

            <button type="button" class="btn-save-master" onclick="saveForm()">Lưu & Kích Hoạt Biểu Mẫu</button>
        </form>
    </div>

    <script>
        const INITIAL_SCHEMA = {schema_json_str};
        let questions = JSON.parse(JSON.stringify(INITIAL_SCHEMA.questions || []));

        function switchTab(tabId) {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

            if (tabId === 'common') {{
                document.getElementById('btnTabCommon').classList.add('active');
                document.getElementById('tabCommon').classList.add('active');
            }} else if (tabId === 'options') {{
                document.getElementById('btnTabOptions').classList.add('active');
                document.getElementById('tabOptions').classList.add('active');
            }} else if (tabId === 'questions') {{
                document.getElementById('btnTabQuestions').classList.add('active');
                document.getElementById('tabQuestions').classList.add('active');
                renderQuestions();
            }}
        }}

        function addQuestion() {{
            questions.push({{
                id: `q_${{Date.now()}}`,
                title: `Câu hỏi số ${{questions.length + 1}}`,
                type: 'text',
                required: true,
                description: '',
                sample_response: '',
                options: []
            }});
            renderQuestions();
        }}

        function removeQuestion(idx) {{
            questions.splice(idx, 1);
            renderQuestions();
        }}

        function toggleQuestionImageChecks(idx, enabled) {{
            if (enabled) {{
                questions[idx].image_checks = {{
                    enable_blur_check: true,
                    blur_threshold: null,
                    enable_ai_vision: true,
                    enable_vehicle_match: true,
                    enable_anti_fraud: true,
                    enable_ai_generated_check: true,
                    enable_screen_photo_check: true
                }};
            }} else {{
                delete questions[idx].image_checks;
            }}
            renderQuestions();
        }}
        function toggleQuestionNumbersOnly(idx, checked) {{
            if (!questions[idx].validation) {{
                questions[idx].validation = {{}};
            }}
            questions[idx].validation.numbers_only = checked;
        }}

        function setQCheck(idx, key, val) {{
            if (!questions[idx].image_checks) {{
                questions[idx].image_checks = {{}};
            }}
            questions[idx].image_checks[key] = val;
        }}
        function escapeHtml(str) {{
            if (str === null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }}
        function renderQuestions() {{
            const wrap = document.getElementById('questionsBuilderList');
            wrap.innerHTML = '';
            questions.forEach((q, idx) => {{
                const item = document.createElement('div');
                item.className = 'question-item';
                item.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-weight:800; font-size:0.92rem; color:#60a5fa;">#${{idx + 1}}. ${{escapeHtml(q.title)}}</span>
                        <button type="button" class="btn-delete" onclick="removeQuestion(${{idx}})">Xóa</button>
                    </div>
                    <div class="row-2">
                        <div class="field-group">
                            <label>Tiêu đề câu hỏi</label>
                            <input type="text" value="${{escapeHtml(q.title)}}" oninput="questions[${{idx}}].title = this.value">
                        </div>
                        <div class="field-group">
                            <label>Loại câu hỏi</label>
                            <select onchange="questions[${{idx}}].type = this.value; renderQuestions();">
                                <option value="text" ${{q.type==='text'?'selected':''}}>Văn bản (Text)</option>
                                <option value="number" ${{q.type==='number'?'selected':''}}>Số (Number)</option>
                                <option value="single_choice" ${{q.type==='single_choice'?'selected':''}}>Chọn một (Single Choice)</option>
                                <option value="dropdown" ${{q.type==='dropdown'?'selected':''}}>Menu thả xuống (Dropdown)</option>
                                <option value="camera_webrtc" ${{q.type==='camera_webrtc'?'selected':''}}>Chụp ảnh trực tiếp (Camera WebRTC)</option>
                                <option value="signature" ${{q.type==='signature'?'selected':''}}>Chữ ký điện tử (Signature)</option>
                            </select>
                        </div>
                    </div>
                    ${{q.type === 'text' ? `
                        <div>
                            <label style="font-size:0.8rem; display:inline-flex; align-items:center; gap:6px; cursor:pointer; color:#93c5fd; margin-top:4px;"><input type="checkbox" class="chk-numbers-only" onchange="toggleQuestionNumbersOnly(${{idx}}, this.checked)" ${{q.validation && q.validation.numbers_only ? 'checked' : ''}}> Chỉ cho phép chữ số (Bàn phím số numeric keypad)</label>
                        </div>
                    ` : ''}}
                    <div class="field-group">
                        <label>Mô tả / Hướng dẫn</label>
                        <input type="text" value="${{escapeHtml(q.description || '')}}" placeholder="Hướng dẫn tài xế điền..." oninput="questions[${{idx}}].description = this.value">
                    </div>
                    ${{['single_choice', 'dropdown'].includes(q.type) ? `
                        <div class="field-group">
                            <label>Các lựa chọn (phân cách bằng dấu phẩy)</label>
                            <input type="text" value="${{escapeHtml((q.options || []).join(', '))}}" placeholder="Lựa chọn 1, Lựa chọn 2, Lựa chọn 3" oninput="questions[${{idx}}].options = this.value.split(',').map(s=>s.trim()).filter(Boolean)">
                        </div>
                    ` : ''}}
                    ${{q.type === 'camera_webrtc' ? `
                        <details style="background:rgba(15, 23, 42, 0.7); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-top:4px;" ${{q.image_checks ? 'open' : ''}}>
                            <summary style="font-size:0.84rem; font-weight:700; color:#60a5fa; cursor:pointer; user-select:none;">
                                ⚙ Tùy chỉnh kiểm tra ảnh riêng cho câu hỏi này
                            </summary>
                            <div style="margin-top:10px; display:flex; flex-direction:column; gap:8px;">
                                <label style="display:flex; align-items:center; gap:8px; font-size:0.8rem; cursor:pointer;">
                                    <input type="checkbox" ${{q.image_checks ? 'checked' : ''}} onchange="toggleQuestionImageChecks(${{idx}}, this.checked)">
                                    <span>Kích hoạt cấu hình kiểm tra riêng (ghi đè cấu hình toàn cục)</span>
                                </label>
                                ${{q.image_checks ? `
                                    <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:8px; margin-top:6px; padding-left:12px; border-left:2px solid var(--accent);">
                                        <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer;">
                                            <input type="checkbox" ${{q.image_checks.enable_blur_check !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_blur_check', this.checked)">
                                            Kiểm tra độ mờ / rung tay
                                        </label>
                                        <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer;">
                                            <input type="checkbox" ${{q.image_checks.enable_ai_vision !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_ai_vision', this.checked)">
                                            Kiểm tra AI Vision
                                        </label>
                                        <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer;">
                                            <input type="checkbox" ${{q.image_checks.enable_vehicle_match !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_vehicle_match', this.checked)">
                                            Đối chiếu biển số
                                        </label>
                                        <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer;">
                                            <input type="checkbox" ${{q.image_checks.enable_anti_fraud !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_anti_fraud', this.checked)">
                                            Chống gian lận & trùng lặp
                                        </label>
                                        <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer;">
                                            <input type="checkbox" ${{q.image_checks.enable_ai_generated_check !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_ai_generated_check', this.checked)">
                                            Kiểm tra ảnh AI tổng hợp
                                        </label>
                                        <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer;">
                                            <input type="checkbox" ${{q.image_checks.enable_screen_photo_check !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_screen_photo_check', this.checked)">
                                            Kiểm tra chụp màn hình
                                        </label>
                                    </div>
                                    <div class="field-group" style="max-width:240px; margin-top:6px; padding-left:12px;">
                                        <label style="font-size:0.75rem;">Ngưỡng độ mờ riêng (mặc định 40)</label>
                                        <input type="number" step="0.1" value="${{q.image_checks.blur_threshold !== undefined && q.image_checks.blur_threshold !== null ? q.image_checks.blur_threshold : 40}}" oninput="setQCheck(${{idx}}, 'blur_threshold', this.value === '' ? null : parseFloat(this.value))">
                                    </div>
                                ` : ''}}
                            </div>
                        </details>
                    ` : ''}}
                `;
                wrap.appendChild(item);
            }});
        }}

        async function saveForm() {{
            const formId = document.getElementById('formId').value.trim();
            const formTitle = document.getElementById('formTitle').value.trim();
            if (!formId || !formTitle) {{
                alert("Vui lòng nhập Mã Form ID và Tên biểu mẫu!");
                switchTab('common');
                return;
            }}

            const payload = {{
                form_id: formId,
                title: formTitle,
                description: document.getElementById('formDesc').value.trim(),
                settings: {{
                    response_limit: parseInt(document.getElementById('responseLimit').value) || null,
                    access_control: document.getElementById('accessControl').value,
                    is_test_mode: document.getElementById('isTestMode').value === 'true',
                    notify_telegram_chat_id: parseInt(document.getElementById('notifyChatId').value) || null,
                    decree13_required: true,
                    image_checks: {{
                        enable_blur_check: document.getElementById('globalEnableBlurCheck') ? document.getElementById('globalEnableBlurCheck').checked : true,
                        blur_threshold: (document.getElementById('globalBlurThreshold') && document.getElementById('globalBlurThreshold').value !== '') ? parseFloat(document.getElementById('globalBlurThreshold').value) : 40.0,
                        enable_ai_vision: document.getElementById('globalEnableAiVision') ? document.getElementById('globalEnableAiVision').checked : true,
                        enable_vehicle_match: document.getElementById('globalEnableVehicleMatch') ? document.getElementById('globalEnableVehicleMatch').checked : true,
                        enable_anti_fraud: document.getElementById('globalEnableAntiFraud') ? document.getElementById('globalEnableAntiFraud').checked : true,
                        enable_ai_generated_check: document.getElementById('globalEnableAiGeneratedCheck') ? document.getElementById('globalEnableAiGeneratedCheck').checked : true,
                        enable_screen_photo_check: document.getElementById('globalEnableScreenPhotoCheck') ? document.getElementById('globalEnableScreenPhotoCheck').checked : false
                    }}
                }},
                questions: questions
            }};

            try {{
                const resp = await fetch('/api/v1/forms/upload-schema', {{
                    method: 'POST',
                    body: JSON.stringify(payload)
                }});
                const res = await resp.json();
                if (resp.ok) {{
                    alert(`Biểu mẫu '${{formTitle}}' đã được lưu thành công!`);
                    window.location.href = '/admin/forms';
                }} else {{
                    alert("Lỗi lưu: " + (res.detail || JSON.stringify(res)));
                }}
            }} catch(e) {{
                alert("Lỗi kết nối máy chủ!");
            }}
        }}

        let currentLang = localStorage.getItem('tb_lang') || 'vi';
        function applyEditorI18n() {{
            document.getElementById('editorLangCode').textContent = currentLang.toUpperCase();
            if (currentLang === 'en') {{
                document.getElementById('linkBackAdmin').textContent = "← Back to Forms List";
            }} else {{
                document.getElementById('linkBackAdmin').textContent = "← Quay lại danh sách biểu mẫu";
            }}
        }}
        function toggleEditorLang() {{
            currentLang = currentLang === 'vi' ? 'en' : 'vi';
            localStorage.setItem('tb_lang', currentLang);
            applyEditorI18n();
        }}
        window.addEventListener('DOMContentLoaded', () => {{
            applyEditorI18n();
            renderQuestions();
        }});
    </script>
</body>
</html>
"""
