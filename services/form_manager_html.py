import html
import json
import math
import os
from typing import Dict, Optional, List, Any
from services.form_engine import FormSchema

DEFAULT_SAMPLE_IMAGES: List[Dict[str, Any]] = [
    {
        "filename": "gsm_real_driver_profile.png",
        "title": "Ảnh 1: Hồ Sơ Tài Xế GSM (Ảnh Thực Tế)",
        "slot_badge": "Slot 1: Hồ Sơ App",
        "dimensions": "1568 × 1568",
        "size_kb": "6.7 MB",
        "desc": "Màn hình hồ sơ ứng dụng tài xế Xanh SM thực tế: Thông tin tài xế, Ví tài xế, Hạng thành viên, Tiện ích bổ trợ.",
    },
    {
        "filename": "gsm_sample_bike.jpg",
        "title": "Ảnh 2: Phương Tiện Vận Hành (Xe Máy Xanh SM)",
        "slot_badge": "Slot 2: Phương Tiện",
        "dimensions": "870 × 1884",
        "size_kb": "106 KB",
        "desc": "Ảnh chụp thực tế phương tiện xe máy điện GSM (VinFast Feliz / Evo 200) phục vụ đối soát.",
    },
    {
        "filename": "gsm_real_week1_income.png",
        "title": "Ảnh 3: Doanh Thu Tuần 07 - 13 (2.890.510 đ)",
        "slot_badge": "Slot 3: Tuần 1",
        "dimensions": "1568 × 1568",
        "size_kb": "10.9 MB",
        "desc": "Báo cáo thu nhập Tuần 07 - 13 Tháng 09: Doanh số 2.828.510 đ, Thu nhập ròng 2.321.732 đ, 65 chuyến hoàn thành.",
    },
    {
        "filename": "gsm_real_week2_income.png",
        "title": "Ảnh 4: Doanh Thu Tuần 14 - 20 (3.934.932 đ)",
        "slot_badge": "Slot 4: Tuần 2",
        "dimensions": "1568 × 1568",
        "size_kb": "5.2 MB",
        "desc": "Báo cáo thu nhập Tuần 14 - 20 Tháng 09: Doanh số 3.860.932 đ, Thu nhập ròng 3.169.166 đ, 80 chuyến hoàn thành.",
    },
    {
        "filename": "gsm_real_week3_income.png",
        "title": "Ảnh 5: Doanh Thu Tuần 28 - 04 (3.653.000 đ)",
        "slot_badge": "Slot 5: Tuần 3",
        "dimensions": "1568 × 1568",
        "size_kb": "6.9 MB",
        "desc": "Báo cáo thu nhập Tuần 28 - 04: Doanh số 3.601.000 đ, Thu nhập ròng 2.955.808 đ, 62 chuyến hoàn thành.",
    },
    {
        "filename": "gsm_sample_car_crop.jpg",
        "title": "Ảnh 6: Đồng Hồ Công Tơ Mét (Odometer)",
        "slot_badge": "Slot 6: Odometer",
        "dimensions": "1164 × 1169",
        "size_kb": "114 KB",
        "desc": "Ảnh chụp thực tế công tơ mét quãng đường xe điện VinFast phục vụ kiểm tra đối soát ca.",
    },
    {
        "filename": "gsm_sample_car_photo.jpg",
        "title": "Ảnh 7: Toàn Cảnh Phương Tiện Vận Hành (Ô Tô GSM)",
        "slot_badge": "Slot 7: Ô Tô GSM",
        "dimensions": "3072 × 3072",
        "size_kb": "3.9 MB",
        "desc": "Ảnh chụp toàn cảnh thân xe và đầu xe taxi điện VinFast VF 5 Plus từ tập dữ liệu GSM thực tế.",
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
                <button type="button" class="btn-action qr" onclick="openAdminQrModal('{f.form_id}', '{html.escape(f.title)}')" title="Mã QR" aria-label="Mã QR">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 3h8v8H3V3zm2 2v4h4V5H5zm8-2h8v8h-8V3zm2 2v4h4V5h-4zM3 13h8v8H3v-8zm2 2v4h4v-4H5zm13-2h3v2h-3v-2zm-3 2h2v3h-2v-3zm3 3h3v3h-3v-3zm-5 1h2v2h-2v-2zm3-3h2v2h-2v-2zM7 7h-.01v.01H7V7zm10 0h-.01v.01H17V7zM7 17h-.01v.01H7V17z"/></svg>
                </button>
                <a href="{form_link}" target="_blank" class="btn-action view" title="Mở Form" aria-label="Mở Form">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 19H5V5h7V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>
                </a>
                <a href="/admin/forms/edit/{f.form_id}" class="btn-action edit" title="Sửa Biểu Mẫu" aria-label="Sửa Biểu Mẫu">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a.996.996 0 0 0 0-1.41l-2.34-2.34a.996.996 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                </a>
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
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400;1,600&display=swap" rel="stylesheet">
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
            font-family: 'Be Vietnam Pro', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
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

        .form-actions {{ display: flex; gap: 8px; margin-top: 10px; }}

        .btn-action {{
            flex: 1;
            height: 38px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 8px;
            text-decoration: none;
            cursor: pointer;
            border: 1px solid transparent;
            transition: all 0.2s ease;
            padding: 0;
        }}
        .btn-action svg {{
            width: 18px;
            height: 18px;
            display: block;
            margin: auto;
        }}
        .btn-action.view {{ background: #2563eb; color: #fff; }}
        .btn-action.view:hover {{ background: #1d4ed8; }}
        .btn-action.edit {{ background: #1e293b; border-color: #334155; color: #cbd5e1; }}
        .btn-action.edit:hover {{ background: #334155; color: #fff; }}
        .btn-action.qr {{ background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); color: #10b981; }}
        .btn-action.qr:hover {{ background: #10b981; color: #fff; }}

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
                <a href="/admin/submissions" class="btn-samples-nav" id="btnAdminSubmissions">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>
                    Dữ Liệu Nộp
                </a>
                <a href="/admin/audit" class="btn-samples-nav" id="btnAdminAudit">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></svg>
                    Nhật Ký Kiểm Toán
                </a>
                <a href="#test-samples-section" class="btn-samples-nav" id="btnAdminSamples">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
                    Ảnh Mẫu Test
                </a>
                <a href="/admin/forms/create" class="btn-create" id="btnAdminCreate">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
                    Tạo Biểu Mẫu Mới
                </a>
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
                    <h2 id="txtSamplesTitle" style="display:flex; align-items:center; gap:8px;">
                        <svg viewBox="0 0 24 24" style="width:20px; height:20px; fill:#38bdf8;"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
                        Kho Ảnh Mẫu Thử Nghiệm
                    </h2>
                    <p id="txtSamplesSub">Tải về máy điện thoại hoặc copy link ảnh chuẩn để kiểm thử các slot camera WebRTC & AI</p>
                </div>
                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                    <a href="/samples/TrustBridge_Test_Image_Set_7Slots.zip" download class="btn-create" style="background:#0284c7; padding:8px 14px; font-size:0.82rem; text-decoration:none;">
                        <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/></svg>
                        Tải Trọn Bộ 7 Ảnh Test (.zip)
                    </a>
                    <span class="badge active">{len(sample_images)} Ảnh Có Sẵn</span>
                </div>
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
                btn.innerHTML = "Đã sao chép link!";
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
                if (btnSamples) btnSamples.textContent = "Test Samples";
                if (txtSTitle) txtSTitle.textContent = "Test Sample Images Gallery";
                if (txtSSub) txtSSub.textContent = "Download to phone camera roll or copy links to test WebRTC camera slots & AI verification";
            }} else {{
                document.getElementById('txtAdminTitle').textContent = "Danh Sách Biểu Mẫu Khảo Sát";
                document.getElementById('txtAdminSub').textContent = "Tổng cộng: {total_forms} biểu mẫu trong hệ thống";
                document.getElementById('btnAdminCreate').textContent = "+ Tạo Biểu Mẫu Mới";
                if (btnSamples) btnSamples.textContent = "Ảnh Mẫu Test";
                if (txtSTitle) txtSTitle.textContent = "Kho Ảnh Mẫu Thử Nghiệm";
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
    <!-- Admin QR Modal Popup -->
    <div id="adminQrModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.8); z-index:9999; align-items:center; justify-content:center; padding:16px;" onclick="if(event.target===this)closeAdminQrModal()">
        <div style="background:#0f172a; border:1px solid #334155; border-top:6px solid #10b981; border-radius:14px; padding:20px; max-width:440px; width:100%; box-shadow:0 25px 50px rgba(0,0,0,0.6);">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:12px; margin-bottom:16px;">
                <h3 id="adminQrModalTitle" style="margin:0; font-size:1.05rem; color:#f8fafc; display:flex; align-items:center; gap:8px;">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
                    <span>Chia Sẻ Mã QR Biểu Mẫu</span>
                </h3>
                <button type="button" onclick="closeAdminQrModal()" style="background:#1e293b; border:1px solid #334155; color:#94a3b8; font-size:1.2rem; cursor:pointer; border-radius:6px; padding:2px 8px; line-height:1;">&times;</button>
            <div style="display:flex; flex-direction:column; align-items:center; gap:16px;">
                <div id="adminQrCodeContainer" style="background:#ffffff; padding:16px; border-radius:12px; display:inline-flex; justify-content:center; align-items:center; box-shadow:0 10px 25px rgba(0,0,0,0.5); min-width:220px; min-height:220px;"></div>
                
                <div style="width:100%;">
                    <div style="font-size:0.75rem; color:#94a3b8; margin-bottom:4px; font-weight:600;">LINK TRUY CẬP (SUBDOMAIN NGẪU NHIÊN):</div>
                    <div style="display:flex; gap:6px;">
                        <input type="text" id="adminQrShareUrlInput" readonly style="flex:1; background:#1e293b; border:1px solid #334155; color:#38bdf8; padding:8px 10px; border-radius:8px; font-size:0.8rem; font-family:monospace; outline:none;" />
                        <button type="button" onclick="regenerateAdminQrSlug()" style="background:#334155; border:none; color:#f8fafc; padding:8px 12px; border-radius:8px; font-size:0.8rem; cursor:pointer; display:flex; align-items:center; justify-content:center;" title="Tạo mã ngẫu nhiên mới">
                            <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
                        </button>
                    </div>
                </div>

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; width:100%;">
                    <button type="button" onclick="copyAdminQrLink()" style="background:#2563eb; color:#fff; border:none; padding:10px; border-radius:8px; font-weight:700; font-size:0.85rem; display:flex; align-items:center; justify-content:center; gap:6px; cursor:pointer;">
                        <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
                        <span>Sao Chép Link</span>
                    </button>
                    <button type="button" onclick="shareAdminQrLink()" style="background:#0284c7; color:#fff; border:none; padding:10px; border-radius:8px; font-weight:700; font-size:0.85rem; display:flex; align-items:center; justify-content:center; gap:6px; cursor:pointer;">
                        <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65 0 1.61 1.31 2.92 2.92 2.92s2.92-1.31 2.92-2.92c0-1.61-1.31-2.92-2.92-2.92z"/></svg>
                        <span>Chia Sẻ</span>
                    </button>
                </div>
                
                <button type="button" onclick="saveAdminQrImage()" style="width:100%; background:rgba(16,185,129,0.15); border:1px solid #10b981; color:#10b981; padding:10px; border-radius:8px; font-weight:700; font-size:0.85rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                    <span>Lưu Ảnh QR (.png)</span>
            </div>
        </div>
    </div>
    <script src="/static/qrcode.min.js"></script>
    <script>
        let currentAdminQrFormId = "";
        let currentAdminQrSlug = "";

        function generateAdminRandomSlug(len = 10) {{
            const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
            let res = "";
            for (let i = 0; i < len; i++) {{
                res += chars.charAt(Math.floor(Math.random() * chars.length));
            }}
            return res;
        }}

        function getAdminFormSubdomainUrl(formId, randSuffix) {{
            const formSlug = (formId || "income_survey").replace(/_/g, "-");
            const host = window.location.hostname;
            let baseDomain = host;
            if (host.includes("sslip.io")) {{
                const parts = host.split(".");
                const ipIndex = parts.findIndex(p => p.match(/^\\d+$/));
                if (ipIndex !== -1 && ipIndex + 3 < parts.length) {{
                    baseDomain = parts.slice(ipIndex).join(".");
                }} else {{
                    baseDomain = "146.190.201.32.sslip.io";
                }}
            }} else if (host.includes("trustbridgevn.com")) {{
                baseDomain = "survey.trustbridgevn.com";
            }}
            const protocol = window.location.protocol;
            return `${{protocol}}//${{formSlug}}-${{randSuffix}}.${{baseDomain}}`;
        }}

        function openAdminQrModal(formId, formTitle) {{
            currentAdminQrFormId = formId;
            currentAdminQrSlug = generateAdminRandomSlug(10);
            const titleEl = document.getElementById("adminQrModalTitle");
            if (titleEl) titleEl.innerHTML = `<span>Mã QR: ${{formTitle}}</span>`;
            const url = getAdminFormSubdomainUrl(currentAdminQrFormId, currentAdminQrSlug);
            const input = document.getElementById("adminQrShareUrlInput");
            if (input) input.value = url;
            const modal = document.getElementById("adminQrModal");
            if (modal) modal.style.display = "flex";
            renderAdminQr(url);
        }}

        function closeAdminQrModal() {{
            const modal = document.getElementById("adminQrModal");
            if (modal) modal.style.display = "none";
        }}

        function regenerateAdminQrSlug() {{
            currentAdminQrSlug = generateAdminRandomSlug(10);
            const url = getAdminFormSubdomainUrl(currentAdminQrFormId, currentAdminQrSlug);
            const input = document.getElementById("adminQrShareUrlInput");
            if (input) input.value = url;
            renderAdminQr(url);
        }}

        function renderAdminQr(url) {{
            const container = document.getElementById("adminQrCodeContainer");
            if (!container) return;
            container.innerHTML = "";
            if (typeof QRCode !== "undefined") {{
                try {{
                    new QRCode(container, {{
                        text: url,
                        width: 220,
                        height: 220,
                        colorDark: "#000000",
                        colorLight: "#ffffff",
                        correctLevel: QRCode.CorrectLevel.H
                    }});
                }} catch(e) {{
                    container.innerHTML = `<img src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${{encodeURIComponent(url)}}" alt="QR Code" style="width:220px; height:220px; display:block;" />`;
                }}
            }} else {{
                container.innerHTML = `<img src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${{encodeURIComponent(url)}}" alt="QR Code" style="width:220px; height:220px; display:block;" />`;
            }}
        }}

        function copyAdminQrLink() {{
            const input = document.getElementById("adminQrShareUrlInput");
            if (!input) return;
            navigator.clipboard.writeText(input.value).then(() => {{
                alert("Đã sao chép liên kết vào bộ nhớ tạm!");
            }}).catch(() => {{
                input.select();
                document.execCommand("copy");
                alert("Đã sao chép liên kết!");
            }});
        }}

        function shareAdminQrLink() {{
            const input = document.getElementById("adminQrShareUrlInput");
            const url = input ? input.value : window.location.href;
            if (navigator.share) {{
                navigator.share({{
                    title: "Khảo sát Trust Bridge",
                    text: "Mời bạn tham gia biểu mẫu khảo sát Trust Bridge",
                    url: url
                }}).catch(() => {{}});
            }} else {{
                copyAdminQrLink();
            }}
        }}

        function saveAdminQrImage() {{
            const container = document.getElementById("adminQrCodeContainer");
            if (!container) return;
            let imgUri = "";
            const canvas = container.querySelector("canvas");
            const img = container.querySelector("img");
            if (canvas) {{
                imgUri = canvas.toDataURL("image/png");
            }} else if (img && img.src) {{
                imgUri = img.src;
            }}
            if (imgUri) {{
                const a = document.createElement("a");
                a.href = imgUri;
                const formSlug = (currentAdminQrFormId || "income_survey").replace(/_/g, "-");
                a.download = `QR_${{formSlug}}_${{currentAdminQrSlug}}.png`;
                a.click();
            }}
        }}
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
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400;1,600&display=swap" rel="stylesheet">
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
            font-family: 'Be Vietnam Pro', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
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

        /* Modern Switch Toggle Component */
        .toggle-switch {{
            position: relative;
            display: inline-flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            user-select: none;
            font-size: 0.8rem;
            color: #cbd5e1;
            font-weight: 600;
        }}

        .toggle-switch input[type="checkbox"] {{
            position: absolute;
            opacity: 0;
            width: 0;
            height: 0;
            pointer-events: none;
        }}

        .toggle-slider {{
            position: relative;
            width: 38px;
            height: 20px;
            background-color: #334155;
            border-radius: 20px;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            border: 1px solid #475569;
            flex-shrink: 0;
        }}

        .toggle-slider::before {{
            content: "";
            position: absolute;
            width: 14px;
            height: 14px;
            left: 2px;
            top: 2px;
            background-color: #f8fafc;
            border-radius: 50%;
            transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
        }}

        .toggle-switch input[type="checkbox"]:checked + .toggle-slider {{
            background-color: var(--accent);
            border-color: #3b82f6;
        }}

        .toggle-switch input[type="checkbox"]:checked + .toggle-slider::before {{
            transform: translateX(18px);
            background-color: #ffffff;
        }}

        .toggle-switch:hover .toggle-slider {{
            border-color: #64748b;
        }}

        .toggle-switch input[type="checkbox"]:checked:hover + .toggle-slider {{
            background-color: var(--accent-hover);
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
                        <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-bottom: 12px;">
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalEnableBlurCheck" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_blur_check', True) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Kiểm tra độ mờ / rung tay</span>
                            </label>
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalEnableAiVision" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_ai_vision', True) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Kiểm tra AI Vision trích xuất hóa đơn</span>
                            </label>
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalEnableVehicleMatch" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_vehicle_match', True) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Kiểm tra đối chiếu biển số</span>
                            </label>
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalEnableAntiFraud" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_anti_fraud', True) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Kiểm tra chống gian lận & trùng lặp</span>
                            </label>
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalRequireUnique" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('require_unique', True) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Yêu cầu ảnh độc nhất (Chống trùng lặp)</span>
                            </label>
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalEnableAiGeneratedCheck" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_ai_generated_check', True) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Kiểm tra ảnh AI tổng hợp</span>
                            </label>
                            <label class="toggle-switch">
                                <input type="checkbox" id="globalEnableScreenPhotoCheck" {'checked' if (schema_dict['settings'].get('image_checks') or {}).get('enable_screen_photo_check', False) else ''}>
                                <span class="toggle-slider"></span>
                                <span>Kiểm tra ảnh chụp màn hình</span>
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

        function slugifyText(str) {{
            return (str || '')
                .toLowerCase()
                .normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '')
                .replace(/[^a-z0-9]+/g, '_')
                .replace(/^_+|_+$/g, '')
                .substring(0, 32);
        }}
        function addQuestion() {{
            const num = questions.length + 1;
            const autoId = `q_${{num}}_${{Date.now().toString(36)}}`;
            questions.push({{
                id: autoId,
                title: `Câu hỏi số ${{num}}`,
                type: 'text',
                required: true,
                description: '',
                tooltip: '',
                ai_detection_prompt: '',
                sample_response: '',
                options: []
            }});
            renderQuestions();
        }}
        function syncQuestionId(idx) {{
            const q = questions[idx];
            if (!q) return;
            const titleSlug = slugifyText(q.title);
            if (titleSlug && (!q.id || q.id.startsWith('q_') || q.id.startsWith('new_'))) {{
                q.id = `${{titleSlug}}_${{idx + 1}}`;
                const idInput = document.getElementById(`qid_${{idx}}`);
                if (idInput) idInput.value = q.id;
            }}
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
        function addImageSlot(qidx) {{
            if (!questions[qidx].image_slots) {{
                questions[qidx].image_slots = [];
            }}
            const sidx = questions[qidx].image_slots.length;
            questions[qidx].image_slots.push({{
                slot: sidx,
                title: `Ảnh ${{sidx + 1}}: `,
                role: 'custom_object',
                required: true,
                require_unique: true,
                allow_upload: true
            }});
            questions[qidx].allow_multiple_images = true;
            questions[qidx].min_images = questions[qidx].image_slots.length;
            questions[qidx].max_images = questions[qidx].image_slots.length;
            questions[qidx].exact_image_count = questions[qidx].image_slots.length;
            renderQuestions();
        }}
        function removeImageSlot(qidx, sidx) {{
            if (questions[qidx].image_slots) {{
                questions[qidx].image_slots.splice(sidx, 1);
                questions[qidx].image_slots.forEach((s, i) => {{ s.slot = i; }});
                questions[qidx].min_images = questions[qidx].image_slots.length;
                questions[qidx].max_images = questions[qidx].image_slots.length;
                questions[qidx].exact_image_count = questions[qidx].image_slots.length;
                renderQuestions();
            }}
        }}
        function updateImageSlot(qidx, sidx, field, val) {{
            if (questions[qidx].image_slots && questions[qidx].image_slots[sidx]) {{
                questions[qidx].image_slots[sidx][field] = val;
            }}
        }}
        function addMultipleChoiceOption(qidx) {{
            if (!Array.isArray(questions[qidx].options)) {{
                questions[qidx].options = [];
            }}
            questions[qidx].options.push({{ label: `Lựa chọn ${{questions[qidx].options.length + 1}}`, group: 'default' }});
            renderQuestions();
        }}
        function removeMultipleChoiceOption(qidx, optIdx) {{
            if (Array.isArray(questions[qidx].options)) {{
                questions[qidx].options.splice(optIdx, 1);
                renderQuestions();
            }}
        }}
        function updateMultipleChoiceOption(qidx, optIdx, field, val) {{
            if (Array.isArray(questions[qidx].options) && questions[qidx].options[optIdx]) {{
                let opt = questions[qidx].options[optIdx];
                if (typeof opt !== 'object' || opt === null) {{
                    opt = {{ label: String(opt), group: 'default' }};
                    questions[qidx].options[optIdx] = opt;
                }}
                opt[field] = val;
            }}
        }}
        function updateGroupCompatibilityJson(qidx, jsonStr) {{
            try {{
                if (!jsonStr || !jsonStr.trim()) {{
                    delete questions[qidx].group_compatibility;
                }} else {{
                    questions[qidx].group_compatibility = JSON.parse(jsonStr);
                }}
            }} catch(e) {{
                // Ignore parse errors while typing
            }}
        }}
        function renderMultipleChoiceOptionsBuilder(q, qidx) {{
            const opts = (q.options || []).map(opt => typeof opt === 'object' && opt !== null ? opt : {{ label: String(opt), group: 'default' }});
            const rows = opts.map((opt, oidx) => `
                <div style="display:flex; gap:8px; align-items:center; margin-bottom:6px;">
                    <input type="text" style="flex:2; padding:6px 10px; font-size:0.82rem; background:#1e293b; border:1px solid #334155; color:#fff; border-radius:4px;" value="${{escapeHtml(opt.label || '')}}" placeholder="Tên lựa chọn (Label)" oninput="updateMultipleChoiceOption(${{qidx}}, ${{oidx}}, 'label', this.value)">
                    <input type="text" style="flex:1; padding:6px 10px; font-size:0.82rem; background:#1e293b; border:1px solid #334155; color:#38bdf8; border-radius:4px; font-family:monospace;" value="${{escapeHtml(opt.group || 'default')}}" placeholder="Nhóm (Group)" oninput="updateMultipleChoiceOption(${{qidx}}, ${{oidx}}, 'group', this.value)">
                    <button type="button" style="background:#ef4444; color:#fff; border:none; padding:4px 8px; border-radius:4px; font-size:0.75rem; cursor:pointer;" onclick="removeMultipleChoiceOption(${{qidx}}, ${{oidx}})">Xóa</button>
                </div>
            `).join('');
            const compatJson = q.group_compatibility ? JSON.stringify(q.group_compatibility, null, 2) : '';
            return `
                <div style="background:rgba(15, 23, 42, 0.75); border:1px solid #334155; border-radius:8px; padding:12px; margin-top:8px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                        <span style="font-size:0.84rem; font-weight:700; color:#38bdf8;">Danh sách Lựa chọn & Nhóm đối kháng (Option Groups)</span>
                        <button type="button" style="padding:4px 10px; font-size:0.75rem; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer;" onclick="addMultipleChoiceOption(${{qidx}})">+ Thêm lựa chọn</button>
                    </div>
                    <div style="display:flex; flex-direction:column;">
                        ${{rows.length ? rows : '<div style="font-size:0.78rem; color:#64748b; font-style:italic;">Chưa có lựa chọn nào.</div>'}}
                    </div>
                    <details style="margin-top:10px; background:#0b1120; border:1px solid #1e293b; border-radius:6px; padding:8px 10px;" ${{q.group_compatibility ? 'open' : ''}}>
                        <summary style="font-size:0.78rem; font-weight:700; color:#93c5fd; cursor:pointer; user-select:none;">
                            Cấu hình nhóm tương thích (Group Compatibility - Advanced)
                        </summary>
                        <div style="margin-top:6px;">
                            <div style="font-size:0.72rem; color:#94a3b8; margin-bottom:4px;">
                                Mặc định các nhóm khác nhau sẽ <b>đối kháng</b> (mutually exclusive). Nhập JSON mapping những nhóm được phép cùng tồn tại, ví dụ: <code>{{"applicable": ["other_group"]}}</code>
                            </div>
                            <textarea style="width:100%; min-height:60px; background:#020617; border:1px solid #334155; color:#38bdf8; font-size:0.75rem; border-radius:4px; padding:6px; font-family:monospace;" placeholder='{{"group_a": ["group_b"]}}' oninput="updateGroupCompatibilityJson(${{qidx}}, this.value)">${{escapeHtml(compatJson)}}</textarea>
                        </div>
                    </details>
                </div>
            `;
        }}
        function renderSlotsBuilderHtml(q, qidx) {{
            const slots = q.image_slots || [];
            if (!slots.length) {{
                return '<div style="font-size:0.78rem; color:#64748b; font-style:italic;">Chưa có slot ảnh riêng (sử dụng 1 ảnh đơn mặc định). Bấm \\'+ Thêm Slot Ảnh\\' để chia nhiều góc chụp/đối tượng.</div>';
            }}
            return slots.map((s, sidx) => `
                <div style="background:#090d16; border:1px solid #334155; border-radius:6px; padding:10px; display:flex; flex-direction:column; gap:6px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-weight:700; font-size:0.8rem; color:#94a3b8;">Slot #${{sidx + 1}} (Chỉ mục: ${{sidx}})</span>
                        <button type="button" style="background:#ef4444; color:#fff; border:none; padding:2px 8px; border-radius:4px; font-size:0.72rem; cursor:pointer;" onclick="removeImageSlot(${{qidx}}, ${{sidx}})">Xóa</button>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
                        <div>
                            <label style="font-size:0.72rem; color:#94a3b8;">Tiêu đề hiển thị</label>
                            <input type="text" style="width:100%; padding:6px; font-size:0.8rem; background:#1e293b; border:1px solid #334155; color:#fff; border-radius:4px;" value="${{escapeHtml(s.title || '')}}" placeholder="VD: Ảnh 1: Mặt trước CCCD" oninput="updateImageSlot(${{qidx}}, ${{sidx}}, 'title', this.value)">
                        </div>
                        <div>
                            <label style="font-size:0.72rem; color:#94a3b8;">Loại đối tượng / Role (Mã AI nhận diện)</label>
                            <input type="text" style="width:100%; padding:6px; font-size:0.8rem; background:#1e293b; border:1px solid #334155; color:#fff; border-radius:4px;" value="${{escapeHtml(s.role || '')}}" placeholder="VD: app_profile, app_income, id_card_front, restaurant_bill..." oninput="updateImageSlot(${{qidx}}, ${{sidx}}, 'role', this.value)">
                        </div>
                    </div>
                    <div>
                        <label style="font-size:0.72rem; color:#94a3b8;">Tooltip hướng dẫn riêng cho slot</label>
                        <input type="text" style="width:100%; padding:6px; font-size:0.8rem; background:#1e293b; border:1px solid #334155; color:#fff; border-radius:4px;" value="${{escapeHtml(s.tooltip || '')}}" placeholder="VD: Chụp rõ họ tên và mã tài xế..." oninput="updateImageSlot(${{qidx}}, ${{sidx}}, 'tooltip', this.value)">
                    </div>
                    <div style="margin-top:4px;">
                        <label style="font-size:0.72rem; color:#fde047; font-weight:700;">AI Detection Prompt riêng cho Slot này (Ẩn khỏi giao diện khảo sát):</label>
                        <textarea style="width:100%; min-height:44px; background:#0f172a; border:1px solid #334155; color:#e2e8f0; font-size:0.75rem; border-radius:4px; padding:6px; font-family:monospace; resize:vertical;" placeholder="Mô tả cho AI biết cần nhận diện đối tượng gì ở ảnh này, kiểm tra tuần hay tháng..." oninput="updateImageSlot(${{qidx}}, ${{sidx}}, 'ai_detection_prompt', this.value)">${{escapeHtml(s.ai_detection_prompt || '')}}</textarea>
                    </div>
                        <label class="toggle-switch">
                            <input type="checkbox" ${{s.required !== false ? 'checked' : ''}} onchange="updateImageSlot(${{qidx}}, ${{sidx}}, 'required', this.checked)">
                            <span class="toggle-slider"></span>
                            <span>Bắt buộc chụp</span>
                        </label>
                        <label class="toggle-switch" title="Bỏ chọn nếu cho phép các ảnh chụp giống nhau (ví dụ: màn hình hồ sơ đã che thông tin / censored profile)">
                            <input type="checkbox" ${{s.require_unique !== false ? 'checked' : ''}} onchange="updateImageSlot(${{qidx}}, ${{sidx}}, 'require_unique', this.checked)">
                            <span class="toggle-slider"></span>
                            <span>Ảnh độc nhất</span>
                        </label>
                        <label class="toggle-switch" title="Cho phép người dùng tải ảnh có sẵn từ bộ sưu tập/file thay vì chỉ chụp camera trực tiếp">
                            <input type="checkbox" ${{s.allow_upload !== false ? 'checked' : ''}} onchange="updateImageSlot(${{qidx}}, ${{sidx}}, 'allow_upload', this.checked)">
                            <span class="toggle-slider"></span>
                            <span>Cho phép tải ảnh lên</span>
                        </label>
                    </div>
                    <details style="margin-top:8px; background:#0b1120; border:1px solid #1e293b; border-radius:6px; padding:8px 10px;" ${{s.image_checks ? 'open' : ''}}>
                        <summary style="font-size:0.76rem; font-weight:700; color:#38bdf8; cursor:pointer; user-select:none; display:flex; align-items:center; gap:6px;">
                            <span>Cấu hình kiểm tra nâng cao riêng cho slot này</span>
                        </summary>
                        <div style="margin-top:8px; display:flex; flex-direction:column; gap:8px;">
                            <label class="toggle-switch" style="font-size:0.78rem;">
                                <input type="checkbox" ${{s.image_checks ? 'checked' : ''}} onchange="toggleSlotImageChecks(${{qidx}}, ${{sidx}}, this.checked)">
                                <span class="toggle-slider"></span>
                                <span>Kích hoạt cấu hình riêng (ghi đè câu hỏi)</span>
                            </label>
                            ${{s.image_checks ? `
                                <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:10px; margin-top:8px; padding-left:10px; border-left:2px solid var(--accent);">
                                    <label class="toggle-switch">
                                        <input type="checkbox" ${{s.image_checks.enable_blur_check !== false ? 'checked' : ''}} onchange="setSlotCheck(${{qidx}}, ${{sidx}}, 'enable_blur_check', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span>Kiểm tra độ mờ / rung</span>
                                    </label>
                                    <label class="toggle-switch">
                                        <input type="checkbox" ${{s.image_checks.enable_ai_vision !== false ? 'checked' : ''}} onchange="setSlotCheck(${{qidx}}, ${{sidx}}, 'enable_ai_vision', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span>Kiểm tra AI Vision</span>
                                    </label>
                                    <label class="toggle-switch">
                                        <input type="checkbox" ${{s.image_checks.enable_vehicle_match !== false ? 'checked' : ''}} onchange="setSlotCheck(${{qidx}}, ${{sidx}}, 'enable_vehicle_match', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span>Đối chiếu biển số</span>
                                    </label>
                                    <label class="toggle-switch">
                                        <input type="checkbox" ${{s.image_checks.enable_anti_fraud !== false ? 'checked' : ''}} onchange="setSlotCheck(${{qidx}}, ${{sidx}}, 'enable_anti_fraud', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span>Chống gian lận & trùng lặp</span>
                                    </label>
                                    <label class="toggle-switch">
                                        <input type="checkbox" ${{s.image_checks.enable_ai_generated_check !== false ? 'checked' : ''}} onchange="setSlotCheck(${{qidx}}, ${{sidx}}, 'enable_ai_generated_check', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span>Kiểm tra ảnh AI</span>
                                    </label>
                                    <label class="toggle-switch">
                                        <input type="checkbox" ${{s.image_checks.enable_screen_photo_check !== false ? 'checked' : ''}} onchange="setSlotCheck(${{qidx}}, ${{sidx}}, 'enable_screen_photo_check', this.checked)">
                                        <span class="toggle-slider"></span>
                                        <span>Kiểm tra chụp màn hình</span>
                                    </label>
                                </div>
                                <div class="field-group" style="max-width:220px; margin-top:6px; padding-left:10px;">
                                    <label style="font-size:0.72rem;">Ngưỡng độ mờ riêng (mặc định 40)</label>
                                    <input type="number" step="0.1" value="${{s.image_checks.blur_threshold !== undefined && s.image_checks.blur_threshold !== null ? s.image_checks.blur_threshold : 40}}" oninput="setSlotCheck(${{qidx}}, ${{sidx}}, 'blur_threshold', this.value === '' ? null : parseFloat(this.value))">
                                </div>
                            ` : ''}}
                        </div>
                    </details>
                </div>
            `).join('');
        }}
        function toggleSlotImageChecks(qidx, sidx, isEnabled) {{
            const slot = questions[qidx]?.image_slots?.[sidx];
            if (!slot) return;
            if (isEnabled) {{
                slot.image_checks = {{
                    enable_blur_check: true,
                    blur_threshold: 40.0,
                    enable_ai_vision: true,
                    enable_vehicle_match: false,
                    enable_anti_fraud: true,
                    enable_ai_generated_check: true,
                    enable_screen_photo_check: false
                }};
            }} else {{
                delete slot.image_checks;
            }}
            renderQuestions();
        }}
        function setSlotCheck(qidx, sidx, key, val) {{
            const slot = questions[qidx]?.image_slots?.[sidx];
            if (!slot) return;
            if (!slot.image_checks) slot.image_checks = {{}};
            slot.image_checks[key] = val;
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
                            <input type="text" value="${{escapeHtml(q.title)}}" oninput="questions[${{idx}}].title = this.value; syncQuestionId(${{idx}});">
                        </div>
                        <div class="field-group">
                            <label>Loại câu hỏi</label>
                            <select onchange="questions[${{idx}}].type = this.value; renderQuestions();">
                                <option value="text" ${{q.type==='text'?'selected':''}}>Văn bản (Text)</option>
                                <option value="number" ${{q.type==='number'?'selected':''}}>Số (Number)</option>
                                <option value="single_choice" ${{q.type==='single_choice'?'selected':''}}>Chọn một (Single Choice)</option>
                                <option value="multiple_choice" ${{q.type==='multiple_choice'?'selected':''}}>Chọn nhiều (Multiple Choice)</option>
                                <option value="dropdown" ${{q.type==='dropdown'?'selected':''}}>Menu thả xuống (Dropdown)</option>
                                <option value="camera_webrtc" ${{q.type==='camera_webrtc'?'selected':''}}>Chụp ảnh trực tiếp (Camera WebRTC)</option>
                                <option value="signature" ${{q.type==='signature'?'selected':''}}>Chữ ký điện tử (Signature)</option>
                            </select>
                        </div>
                    </div>
                    <div class="row-2" style="margin-top:6px;">
                        <div class="field-group">
                            <label style="display:flex; justify-content:space-between; align-items:center;">
                                <span>Mã Question ID (Tự động tạo)</span>
                                <span style="font-size:0.7rem; color:#94a3b8; font-weight:normal;">Định danh duy nhất</span>
                            </label>
                            <input type="text" id="qid_${{idx}}" value="${{escapeHtml(q.id || '')}}" placeholder="vd: driver_phone, camera_receipt..." oninput="questions[${{idx}}].id = this.value.trim()">
                        </div>
                        <div class="field-group">
                            <label>Tooltip giải thích (Hiển thị popup khi rê chuột)</label>
                            <input type="text" value="${{escapeHtml(q.tooltip || '')}}" placeholder="Thông tin trợ giúp nhanh..." oninput="questions[${{idx}}].tooltip = this.value">
                        </div>
                    </div>
                    <div>
                        <label class="toggle-switch" style="margin-top:4px;">
                            <input type="checkbox" ${{q.required !== false ? 'checked' : ''}} onchange="questions[${{idx}}].required = this.checked;">
                            <span class="toggle-slider"></span>
                            <span style="color:#38bdf8;">Bắt buộc trả lời (Mandatory Question)</span>
                        </label>
                    </div>
                    ${{q.type === 'camera_webrtc' ? `
                        <div class="field-group" style="margin-top:6px; background:rgba(30, 41, 59, 0.4); border:1px dashed #475569; border-radius:6px; padding:8px 10px;">
                            <label style="color:#fde047; font-size:0.75rem; font-weight:700;">AI Detection Prompt (Mô tả riêng cho AI - Ẩn khỏi giao diện khảo sát):</label>
                            <textarea style="width:100%; min-height:48px; background:#0f172a; border:1px solid #334155; color:#e2e8f0; font-size:0.78rem; border-radius:4px; padding:6px; font-family:monospace; resize:vertical;" placeholder="Mô tả cho AI biết cần nhận diện đối tượng gì, chữ số nào, điều kiện duyệt..." oninput="questions[${{idx}}].ai_detection_prompt = this.value">${{escapeHtml(q.ai_detection_prompt || '')}}</textarea>
                        </div>
                    ` : ''}}
                    ${{q.type === 'text' ? `
                        <div>
                            <label style="font-size:0.8rem; display:inline-flex; align-items:center; gap:6px; cursor:pointer; color:#93c5fd; margin-top:4px;"><input type="checkbox" class="chk-numbers-only" onchange="toggleQuestionNumbersOnly(${{idx}}, this.checked)" ${{q.validation && q.validation.numbers_only ? 'checked' : ''}}> Chỉ cho phép chữ số (Bàn phím số numeric keypad)</label>
                        </div>
                    ` : ''}}
                    <div class="field-group">
                        <label style="display:flex; justify-content:space-between; align-items:center;">
                            <span>Mô tả / Hướng dẫn</span>
                            <span style="font-size:0.7rem; color:#38bdf8; font-weight:normal;">Hỗ trợ chèn link [Tên link](url) và ảnh ![Mô tả](url)</span>
                        </label>
                        <input type="text" value="${{escapeHtml(q.description || '')}}" placeholder="Hướng dẫn tài xế điền..." oninput="questions[${{idx}}].description = this.value">
                    </div>
                    ${{q.type === 'multiple_choice' ? renderMultipleChoiceOptionsBuilder(q, idx) : (['single_choice', 'dropdown'].includes(q.type) ? `
                        <div class="field-group">
                            <label>Các lựa chọn (phân cách bằng dấu phẩy)</label>
                            <input type="text" value="${{escapeHtml((q.options || []).map(o => typeof o === 'object' && o !== null ? o.label : o).join(', '))}}" placeholder="Lựa chọn 1, Lựa chọn 2, Lựa chọn 3" oninput="questions[${{idx}}].options = this.value.split(',').map(s=>s.trim()).filter(Boolean)">
                        </div>
                    ` : '')}}
                    <div style="display:none;" data-options-marker="true" value="${{escapeHtml((q.options || []).join(', '))}}"></div>
                    ${{q.type === 'camera_webrtc' ? `
                        <div style="background:rgba(15, 23, 42, 0.75); border:1px solid #334155; border-radius:8px; padding:12px; margin-top:8px;">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                                <span style="font-size:0.86rem; font-weight:800; color:#38bdf8;">Cấu hình các Slot Ảnh & Loại Đối Tượng (Image Slots & Object Roles)</span>
                                <button type="button" class="btn-action view" style="padding:4px 10px; font-size:0.75rem; background:#2563eb; color:#fff; border:none; border-radius:6px; cursor:pointer;" onclick="addImageSlot(${{idx}})">+ Thêm Slot Ảnh</button>
                            </div>
                            <div id="slotsWrap_${{idx}}" style="display:flex; flex-direction:column; gap:8px;">
                                ${{renderSlotsBuilderHtml(q, idx)}}
                            </div>
                        </div>
                        <details style="background:rgba(15, 23, 42, 0.7); border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-top:4px;" ${{q.image_checks ? 'open' : ''}}>
                            <summary style="font-size:0.84rem; font-weight:700; color:#60a5fa; cursor:pointer; user-select:none;">
                                Tùy chỉnh kiểm tra ảnh riêng cho câu hỏi này
                            </summary>
                            <div style="margin-top:10px; display:flex; flex-direction:column; gap:8px;">
                                <label class="toggle-switch" style="font-size:0.82rem;">
                                    <input type="checkbox" ${{q.image_checks ? 'checked' : ''}} onchange="toggleQuestionImageChecks(${{idx}}, this.checked)">
                                    <span class="toggle-slider"></span>
                                    <span>Kích hoạt cấu hình kiểm tra riêng (ghi đè toàn cục)</span>
                                </label>
                                ${{q.image_checks ? `
                                    <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:10px; margin-top:8px; padding-left:12px; border-left:2px solid var(--accent);">
                                        <label class="toggle-switch">
                                            <input type="checkbox" ${{q.image_checks.enable_blur_check !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_blur_check', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Kiểm tra độ mờ / rung tay</span>
                                        </label>
                                        <label class="toggle-switch">
                                            <input type="checkbox" ${{q.image_checks.enable_ai_vision !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_ai_vision', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Kiểm tra AI Vision</span>
                                        </label>
                                        <label class="toggle-switch">
                                            <input type="checkbox" ${{q.image_checks.enable_vehicle_match !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_vehicle_match', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Đối chiếu biển số</span>
                                        </label>
                                        <label class="toggle-switch">
                                            <input type="checkbox" ${{q.image_checks.enable_anti_fraud !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_anti_fraud', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Chống gian lận & trùng lặp</span>
                                        </label>
                                        <label class="toggle-switch" title="Bỏ chọn nếu cho phép ảnh chụp giống nhau (ví dụ: hồ sơ che mờ)">
                                            <input type="checkbox" ${{q.image_checks.require_unique !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'require_unique', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Ảnh độc nhất (Unique)</span>
                                        </label>
                                        <label class="toggle-switch">
                                            <input type="checkbox" ${{q.image_checks.enable_ai_generated_check !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_ai_generated_check', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Kiểm tra ảnh AI</span>
                                        </label>
                                        <label class="toggle-switch">
                                            <input type="checkbox" ${{q.image_checks.enable_screen_photo_check !== false ? 'checked' : ''}} onchange="setQCheck(${{idx}}, 'enable_screen_photo_check', this.checked)">
                                            <span class="toggle-slider"></span>
                                            <span>Kiểm tra chụp màn hình</span>
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
                        require_unique: document.getElementById('globalRequireUnique') ? document.getElementById('globalRequireUnique').checked : true,
                        enable_ai_generated_check: document.getElementById('globalEnableAiGeneratedCheck') ? document.getElementById('globalEnableAiGeneratedCheck').checked : true,
                        enable_screen_photo_check: document.getElementById('globalEnableScreenPhotoCheck') ? document.getElementById('globalEnableScreenPhotoCheck').checked : false
                    }}
                }},
                questions: questions.map(q => ({{
                    ...q,
                    required: q.required !== false
                }}))
            }};

            try {{
                const resp = await fetch('/api/v1/forms/upload-schema', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    credentials: 'include',
                    body: JSON.stringify(payload)
                }});
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
