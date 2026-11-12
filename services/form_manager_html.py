# -*- coding: utf-8 -*-
"""
Forms Manager & Visual 3-Tab Editor HTML Rendering Service.
Provides expanded 1440px layout, Trust Bridge branded header,
grid/row view mode toggles, 3-line title truncation, live filter/sort bar,
page-size selector, and a clean canonical QR code sharing popup.
"""

import html
import json
import math
import os
from typing import Dict, Optional, List, Any
from services.form_engine import FormSchema, FormSettings, FormQuestion, QuestionType

# =========================================================================================
# NOTE / IMPORTANT RELEASE GATE:
# DUMMY_DEV_FORMS below are STRICTLY for development staging UI layout & pagination testing.
# They MUST be removed or stripped before promoting the dev branch to production master!
# =========================================================================================
DUMMY_DEV_FORMS: List[Dict[str, Any]] = [
    {
        "form_id": "placeholder_1",
        "title": "Placeholder 1",
        "description": "Biểu mẫu mẫu để kiểm thử giao diện phân trang và hiển thị lưới. Bắt buộc xóa khi đẩy lên production.",
        "question_count": 8,
        "is_test_mode": False,
        "access_control": "public"
    },
    {
        "form_id": "placeholder_2",
        "title": "Placeholder 2",
        "description": "Biểu mẫu mẫu để kiểm thử giao diện phân trang và hiển thị lưới. Bắt buộc xóa khi đẩy lên production.",
        "question_count": 12,
        "is_test_mode": True,
        "access_control": "public"
    },
    {
        "form_id": "placeholder_3",
        "title": "Placeholder 3",
        "description": "Biểu mẫu mẫu để kiểm thử giao diện phân trang và hiển thị lưới. Bắt buộc xóa khi đẩy lên production.",
        "question_count": 6,
        "is_test_mode": False,
        "access_control": "public"
    },
    {
        "form_id": "placeholder_4",
        "title": "Placeholder 4",
        "description": "Biểu mẫu mẫu để kiểm thử giao diện phân trang và hiển thị lưới. Bắt buộc xóa khi đẩy lên production.",
        "question_count": 5,
        "is_test_mode": True,
        "access_control": "public"
    },
    {
        "form_id": "placeholder_5",
        "title": "Placeholder 5",
        "description": "Biểu mẫu mẫu để kiểm thử giao diện phân trang và hiển thị lưới. Bắt buộc xóa khi đẩy lên production.",
        "question_count": 7,
        "is_test_mode": False,
        "access_control": "public"
    }
]


def render_forms_list_html(
    forms_registry: Dict[str, FormSchema],
    page: int = 1,
    page_size: int = 6,
    environment: str = "production"
) -> str:
    is_dev = (str(environment).lower() != "production")
    if is_dev:
        env_badge = '<span style="display:inline-flex; align-items:center; gap:5px; background:rgba(245,158,11,0.18); color:#f59e0b; border:1px solid rgba(245,158,11,0.45); font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:9999px; vertical-align:middle; margin-left:8px; text-transform:uppercase; letter-spacing:0.5px;">⚡ DEV REPLICA (surveydb_dev)</span>'
        env_subtext = "Môi trường Thử Nghiệm / Staging độc lập"
    else:
        env_badge = '<span style="display:inline-flex; align-items:center; gap:5px; background:rgba(16,185,129,0.15); color:#10b981; border:1px solid rgba(16,185,129,0.35); font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:9999px; vertical-align:middle; margin-left:8px; text-transform:uppercase; letter-spacing:0.5px;">✓ PRODUCTION</span>'
        env_subtext = "Hệ thống Cơ sở dữ liệu chính thức"

    unique_forms: Dict[str, Any] = {}
    for f in forms_registry.values():
        unique_forms[f.form_id] = {
            "form_id": f.form_id,
            "title": f.title,
            "description": f.description or "",
            "question_count": len(f.questions),
            "is_test_mode": bool(f.settings and f.settings.is_test_mode),
            "access_control": f.settings.access_control if f.settings else "public"
        }

    # In dev build, seed dummy forms for rich UI testing
    if is_dev:
        for dummy in DUMMY_DEV_FORMS:
            if dummy["form_id"] not in unique_forms:
                unique_forms[dummy["form_id"]] = dummy

    all_forms = list(unique_forms.values())
    total_forms = len(all_forms)
    page_size = max(1, page_size)
    total_pages = max(1, math.ceil(total_forms / page_size))
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * page_size
    paged_forms = all_forms[start_idx : start_idx + page_size]

    forms_cards_html = ""
    for f in paged_forms:
        is_test = f["is_test_mode"]
        badge_cls = "badge test" if is_test else "badge active"
        badge_txt = "Thử nghiệm" if is_test else "Chính thức"
        form_id_str = f["form_id"]
        form_link = f"/forms/{form_id_str}?mode=test" if is_test else f"/forms/{form_id_str}"
        title_esc = html.escape(f["title"])

        forms_cards_html += f"""
        <div class="form-item-card" data-form-id="{form_id_str}" data-title="{title_esc.lower()}" data-status="{'test' if is_test else 'active'}" data-questions="{f['question_count']}">
            <div class="form-card-top">
                <span class="{badge_cls}">{badge_txt}</span>
                <span style="font-size:0.75rem; color:var(--muted); font-family:monospace;">ID: {html.escape(form_id_str)}</span>
            </div>
            <h3 title="{title_esc}">{title_esc}</h3>
            <div class="form-item-meta">
                <div>Số câu hỏi: <b>{f['question_count']}</b></div>
                <div>Quyền truy cập: <b>{html.escape(f['access_control'])}</b></div>
            </div>
            <div class="form-actions">
                <button type="button" class="btn-action qr" onclick="openAdminQrModal('{form_id_str}', '{title_esc}')" title="Mã QR" aria-label="Mã QR">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 3h8v8H3V3zm2 2v4h4V5H5zm8-2h8v8h-8V3zm2 2v4h4V5h-4zM3 13h8v8H3v-8zm2 2v4h4v-4H5zm13-2h3v2h-3v-2zm-3 2h2v3h-2v-3zm3 3h3v3h-3v-3zm-5 1h2v2h-2v-2zm3-3h2v2h-2v-2zM7 7h-.01v.01H7V7zm10 0h-.01v.01H17V7zM7 17h-.01v.01H7V17z"/></svg>
                </button>
                <a href="{form_link}" target="_blank" class="btn-action view" title="Mở Form" aria-label="Mở Form">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 19H5V5h7V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>
                </a>
                <a href="/admin/forms/edit/{form_id_str}" class="btn-action edit" title="Sửa Biểu Mẫu" aria-label="Sửa Biểu Mẫu">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a.996.996 0 0 0 0-1.41l-2.34-2.34a.996.996 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                </a>
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
    <title>Trust Bridge - Quản Lý Biểu Mẫu</title>
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
            --text: #f8fafc;
            --muted: #94a3b8;
            --card-radius: 12px;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: 'Be Vietnam Pro', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px 16px;
        }}

        /* Expanded container: >40% wider than 900px */
        .container {{
            max-width: 1440px;
            width: 96%;
            margin: 0 auto;
        }}

        /* Top Branded Trust Bridge Header */
        .brand-banner {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.9) 100%);
            border: 1px solid var(--border);
            border-radius: var(--card-radius);
            padding: 16px 22px;
            margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
            flex-wrap: wrap;
            gap: 14px;
        }}

        .brand-banner-left {{
            display: flex;
            align-items: center;
            gap: 14px;
        }}

        .brand-logo-wrap {{
            width: 44px;
            height: 44px;
            background: rgba(37, 99, 235, 0.15);
            border: 1px solid rgba(59, 130, 246, 0.4);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .brand-banner-title h1 {{
            font-size: 1.25rem;
            font-weight: 800;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .brand-banner-title p {{
            font-size: 0.8rem;
            color: var(--muted);
            margin-top: 2px;
        }}

        .nav-actions {{
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }}

        .nav-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            text-decoration: none;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .nav-btn:hover {{
            background: var(--surface-hover);
            border-color: #3b82f6;
            color: #60a5fa;
        }}

        .nav-btn.primary {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}

        .nav-btn.primary:hover {{
            background: var(--accent-hover);
        }}

        /* Filter & Sorting Bar Above Forms List */
        .filter-toolbar {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--card-radius);
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 18px;
            flex-wrap: wrap;
        }}

        .search-wrap {{
            flex: 1;
            min-width: 240px;
            position: relative;
        }}

        .search-input {{
            width: 100%;
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 12px 8px 34px;
            color: #fff;
            font-size: 0.82rem;
            outline: none;
        }}

        .search-input:focus {{
            border-color: #3b82f6;
        }}

        .search-icon {{
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 16px;
            height: 16px;
            fill: var(--muted);
            pointer-events: none;
        }}

        .filter-controls {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }}

        .select-filter {{
            background: #090d16;
            border: 1px solid var(--border);
            color: #fff;
            padding: 7px 10px;
            border-radius: 8px;
            font-size: 0.8rem;
            outline: none;
            cursor: pointer;
        }}

        .view-btn {{
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 0.78rem;
        }}

        .view-btn.active {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}

        /* Forms Layout: Grid Mode with equal height rows */
        .form-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
            align-items: stretch;
        }}

        .form-item-card {{
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 12px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            gap: 12px;
            transition: transform 0.15s ease, border-color 0.15s ease;
        }}

        .form-item-card:hover {{
            border-color: #3b82f6;
            transform: translateY(-2px);
        }}

        .form-card-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        /* Title Capped to exactly 3 lines with ellipsis, fixed min-height for row alignment */
        .form-item-card h3 {{
            font-size: 0.98rem;
            font-weight: 700;
            color: #fff;
            line-height: 1.4;
            min-height: 4.2em;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .form-item-meta {{
            font-size: 0.8rem;
            color: var(--muted);
            display: flex;
            flex-direction: column;
            gap: 4px;
            background: rgba(15, 23, 42, 0.4);
            padding: 8px 10px;
            border-radius: 6px;
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

        .form-actions {{
            display: flex;
            gap: 8px;
            margin-top: 6px;
        }}

        .btn-action {{
            flex: 1;
            height: 36px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 6px;
            color: #fff;
            text-decoration: none;
            border: 1px solid var(--border);
            cursor: pointer;
            transition: all 0.15s ease;
            background: var(--surface-hover);
        }}

        .btn-action.qr:hover {{ background: #0284c7; border-color: #0284c7; }}
        .btn-action.view:hover {{ background: #10b981; border-color: #10b981; }}
        .btn-action.edit:hover {{ background: #2563eb; border-color: #2563eb; }}
        .btn-action svg {{ width: 16px; height: 16px; }}

        /* Row Mode (List View) */
        .form-grid.row-mode {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}

        .form-grid.row-mode .form-item-card {{
            flex-direction: row;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 12px 18px;
        }}

        .form-grid.row-mode .form-card-top {{
            flex-direction: column;
            align-items: flex-start;
            gap: 4px;
            min-width: 110px;
        }}

        .form-grid.row-mode .form-item-card h3 {{
            min-height: auto;
            -webkit-line-clamp: 1;
            flex: 2;
        }}

        .form-grid.row-mode .form-item-meta {{
            flex: 2;
            flex-direction: row;
            gap: 16px;
            align-items: center;
            background: none;
            padding: 0;
        }}

        .form-grid.row-mode .form-actions {{
            flex: 1;
            margin-top: 0;
            max-width: 150px;
        }}

        /* Pagination & Page Size Row */
        .pagination-row {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--card-radius);
            padding: 12px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .pagination-controls {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .btn-page {{
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: #fff;
            padding: 6px 12px;
            border-radius: 6px;
            text-decoration: none;
            font-size: 0.82rem;
            font-weight: 600;
        }}

        .btn-page.disabled {{
            opacity: 0.4;
            pointer-events: none;
        }}

        .page-indicator {{
            font-size: 0.82rem;
            color: var(--muted);
        }}

        .page-size-wrap {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.8rem;
            color: var(--muted);
        }}

        /* Clean Canonical QR Code Modal */
        .modal-overlay {{
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.82);
            backdrop-filter: blur(4px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            padding: 16px;
        }}

        .modal-card {{
            background: #0f172a;
            border: 1px solid var(--border);
            border-top: 5px solid #10b981;
            border-radius: 16px;
            max-width: 440px;
            width: 100%;
            padding: 22px;
            box-shadow: 0 25px 50px rgba(0,0,0,0.7);
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .modal-header-row {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
        }}

        function escapeHtml(str) {{
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }}
        .modal-title-group h3 {{
            font-size: 1.05rem;
            font-weight: 800;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .modal-title-group p {{
            font-size: 0.8rem;
            color: var(--muted);
            margin-top: 3px;
        }}

        .btn-close-modal {{
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--muted);
            font-size: 1.3rem;
            cursor: pointer;
            border-radius: 6px;
            padding: 2px 8px;
            line-height: 1;
        }}

        .btn-close-modal:hover {{ color: #fff; }}

        .qr-canvas-box {{
            background: #ffffff;
            padding: 16px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            align-self: center;
            box-shadow: 0 8px 25px rgba(0,0,0,0.5);
            min-width: 220px;
            min-height: 220px;
        }}

        .url-box {{
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 10px;
            color: #38bdf8;
            font-family: monospace;
            font-size: 0.8rem;
            width: 100%;
            outline: none;
        }}

        .modal-btn-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }}

        .btn-modal-action {{
            padding: 10px;
            border-radius: 8px;
            font-weight: 700;
            font-size: 0.82rem;
            border: none;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Top Branded Trust Bridge Header -->
        <div class="brand-banner">
            <div class="brand-banner-left">
                <div class="brand-logo-wrap">
                    <svg viewBox="0 0 24 24" style="width:24px; height:24px; fill:#38bdf8;"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></svg>
                </div>
                <div class="brand-banner-title">
                    <h1 id="txtAdminTitle">
                        Quản Lý Biểu Mẫu Khảo Sát
                        {env_badge}
                    </h1>
                    <p id="txtAdminSub">Hệ Thống Trust Bridge SSOT: Cấu hình biểu mẫu, cấp mã QR chia sẻ, và định tuyến đối soát ({env_subtext})</p>
                </div>
            </div>
            <div class="nav-actions">
                <a href="/admin/submissions" class="nav-btn" id="btnAdminSubmissions">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>
                    Dữ Liệu Nộp
                </a>
                <a href="/admin/audit" class="nav-btn" id="btnAdminAudit">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></svg>
                    Nhật Ký Kiểm Toán
                </a>
                <a href="/admin/media" class="nav-btn" id="btnAdminMedia">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zm-5.04-6.71l-2.75 3.54-1.96-2.36L6.5 17h11l-3.54-4.71z"/></svg>
                    Quản Lý Media
                </a>
                <a href="/admin/forms/create" class="nav-btn primary" id="btnAdminCreate">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
                    Tạo Biểu Mẫu Mới
                </a>
            </div>
        </div>

        <!-- Filter & Sorting Bar Above Forms List -->
        <div class="filter-toolbar">
            <div class="search-wrap">
                <svg class="search-icon" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 14z"/></svg>
                <input type="text" id="filterSearchInput" class="search-input" placeholder="Lọc biểu mẫu theo tên hoặc mã form..." oninput="applyClientFilter()">
            </div>
            <div class="filter-controls">
                <select id="statusFilter" class="select-filter" onchange="applyClientFilter()">
                    <option value="all">Tất cả trạng thái</option>
                    <option value="active">Chính thức (Active)</option>
                    <option value="test">Thử nghiệm (Test)</option>
                </select>
                <select id="sortFilter" class="select-filter" onchange="applyClientFilter()">
                    <option value="default">Sắp xếp: Mặc định</option>
                    <option value="name_asc">Tên (A → Z)</option>
                    <option value="name_desc">Tên (Z → A)</option>
                    <option value="questions_desc">Số câu hỏi (Nhiều nhất)</option>
                </select>
                <button type="button" class="view-btn active" id="btnViewGrid" onclick="setFormViewMode('grid')">
                    <svg viewBox="0 0 24 24" style="width:14px; height:14px; fill:currentColor;"><path d="M3 3v8h8V3H3zm6 6H5V5h4v4zm-6 4v8h8v-8H3zm6 6H5v-4h4v4zm4-16v8h8V3h-8zm6 6h-4V5h4v4zm-6 4v8h8v-8h-8zm6 6h-4v-4h4v4z"/></svg>
                    Lưới
                </button>
                <button type="button" class="view-btn" id="btnViewRow" onclick="setFormViewMode('row')">
                    <svg viewBox="0 0 24 24" style="width:14px; height:14px; fill:currentColor;"><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg>
                    Dòng
                </button>
            </div>
        </div>

        <!-- Dynamic Form Grid / Rows -->
        <div class="form-grid" id="formsContainer">
            {forms_cards_html}
        </div>

        <!-- Pagination Controls Row with Page Size Selector -->
        <div class="pagination-row">
            <div class="pagination-controls">
                <a href="/admin/forms?page={current_page - 1}&size={page_size}" class="btn-page {prev_disabled}">Trang trước</a>
                <span class="page-indicator">Trang {current_page} / {total_pages} (Tổng {total_forms} biểu mẫu)</span>
                <a href="/admin/forms?page={current_page + 1}&size={page_size}" class="btn-page {next_disabled}">Trang sau</a>
            </div>
            <div class="page-size-wrap">
                <span>Số lượng hiển thị:</span>
                <select id="pageSizeSelect" class="select-filter" onchange="changePageSize(this.value)">
                    <option value="6" {'selected' if page_size == 6 else ''}>6 / trang</option>
                    <option value="12" {'selected' if page_size == 12 else ''}>12 / trang</option>
                    <option value="24" {'selected' if page_size == 24 else ''}>24 / trang</option>
                    <option value="50" {'selected' if page_size == 50 else ''}>50 / trang</option>
                </select>
            </div>
        </div>
    </div>

    <!-- Redesigned Clean Canonical QR Code Modal Popup -->
    <div id="adminQrModal" class="modal-overlay" onclick="if(event.target===this)closeAdminQrModal()">
        <div class="modal-card">
            <div class="modal-header-row">
                <div class="modal-title-group">
                    <h3 id="adminQrModalTitle">
                        <svg viewBox="0 0 24 24" style="width:20px; height:20px; fill:#10b981;"><path d="M3 3h8v8H3V3zm2 2v4h4V5H5zm8-2h8v8h-8V3zm2 2v4h4V5h-4zM3 13h8v8H3v-8zm2 2v4h4v-4H5zm13-2h3v2h-3v-2zm-3 2h2v3h-2v-3zm3 3h3v3h-3v-3zm-5 1h2v2h-2v-2zm3-3h2v2h-2v-2zM7 7h-.01v.01H7V7zm10 0h-.01v.01H17V7zM7 17h-.01v.01H7V17z"/></svg>
                        Chia Sẻ Mã QR Biểu Mẫu
                    </h3>
                    <p id="adminQrFormTitle" style="color:#60a5fa; font-weight:600;"></p>
                </div>
                <button type="button" class="btn-close-modal" onclick="closeAdminQrModal()">&times;</button>
            </div>

            <!-- Centered QR Code -->
            <div id="adminQrCodeContainer" class="qr-canvas-box"></div>

            <!-- Direct Canonical Form URL (No Random Subdomain) -->
            <div>
                <label style="font-size:0.75rem; color:var(--muted); font-weight:700; margin-bottom:4px; display:block;">ĐƯỜNG DẪN BIỂU MẪU CHÍNH THỨC</label>
                <input type="text" id="adminQrShareUrlInput" class="url-box" readonly />
            </div>

            <!-- Action Buttons -->
            <div class="modal-btn-grid">
                <button type="button" onclick="copyAdminQrLink()" class="btn-modal-action" style="background:#2563eb; color:#fff;">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
                    <span>Sao Chép Link</span>
                </button>
                <button type="button" onclick="saveAdminQrImage()" class="btn-modal-action" style="background:rgba(16,185,129,0.18); border:1px solid #10b981; color:#10b981;">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM17 13l-5 5-5-5h3V9h4v4h3z"/></svg>
                    <span>Lưu Ảnh QR (.png)</span>
                </button>
            </div>
        </div>
    </div>

    <script src="/static/qrcode.min.js"></script>
    <script>
        let currentAdminQrFormId = "";

        function setFormViewMode(mode) {{
            const container = document.getElementById("formsContainer");
            const btnGrid = document.getElementById("btnViewGrid");
            const btnRow = document.getElementById("btnViewRow");
            if (mode === "row") {{
                container.classList.add("row-mode");
                btnRow.classList.add("active");
                btnGrid.classList.remove("active");
                localStorage.setItem("tb_forms_view", "row");
            }} else {{
                container.classList.remove("row-mode");
                btnGrid.classList.add("active");
                btnRow.classList.remove("active");
                localStorage.setItem("tb_forms_view", "grid");
            }}
        }}

        function applyClientFilter() {{
            const q = (document.getElementById("filterSearchInput").value || "").toLowerCase().trim();
            const status = document.getElementById("statusFilter").value;
            const sort = document.getElementById("sortFilter").value;
            const cards = Array.from(document.querySelectorAll(".form-item-card"));

            cards.forEach(card => {{
                const title = card.getAttribute("data-title") || "";
                const formId = card.getAttribute("data-form-id") || "";
                const cStatus = card.getAttribute("data-status") || "";

                const matchQ = !q || title.includes(q) || formId.includes(q);
                const matchStatus = (status === "all") || (cStatus === status);

                if (matchQ && matchStatus) {{
                    card.style.display = "flex";
                }} else {{
                    card.style.display = "none";
                }}
            }});

            // Sorting
            const container = document.getElementById("formsContainer");
            const visibleCards = cards.filter(c => c.style.display !== "none");
            if (sort === "name_asc") {{
                visibleCards.sort((a, b) => (a.getAttribute("data-title") || "").localeCompare(b.getAttribute("data-title") || ""));
            }} else if (sort === "name_desc") {{
                visibleCards.sort((a, b) => (b.getAttribute("data-title") || "").localeCompare(a.getAttribute("data-title") || ""));
            }} else if (sort === "questions_desc") {{
                visibleCards.sort((a, b) => parseInt(b.getAttribute("data-questions") || "0") - parseInt(a.getAttribute("data-questions") || "0"));
            }}
            visibleCards.forEach(c => container.appendChild(c));
        }}

        function changePageSize(size) {{
            const url = new URL(window.location.href);
            url.searchParams.set("size", size);
            url.searchParams.set("page", "1");
            window.location.href = url.toString();
        }}

        function openAdminQrModal(formId, formTitle) {{
            currentAdminQrFormId = formId;
            document.getElementById("adminQrFormTitle").textContent = formTitle;
            
            // Canonical Form URL (No random subdomains)
            const canonicalUrl = window.location.origin + "/forms/" + encodeURIComponent(formId);
            document.getElementById("adminQrShareUrlInput").value = canonicalUrl;
            
            const modal = document.getElementById("adminQrModal");
            modal.style.display = "flex";
            renderAdminQr(canonicalUrl);
        }}

        function closeAdminQrModal() {{
            document.getElementById("adminQrModal").style.display = "none";
        }}

        function renderAdminQr(text) {{
            const container = document.getElementById("adminQrCodeContainer");
            container.innerHTML = "";
            if (typeof QRCode !== 'undefined') {{
                new QRCode(container, {{
                    text: text,
                    width: 220,
                    height: 220,
                    colorDark: "#090d16",
                    colorLight: "#ffffff",
                    correctLevel: QRCode.CorrectLevel.M
                }});
            }} else {{
                container.innerHTML = `<img src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${{encodeURIComponent(text)}}" alt="QR Code" style="width:220px;height:220px;">`;
            }}
        }}

        function copyAdminQrLink() {{
            const input = document.getElementById("adminQrShareUrlInput");
            if (input) {{
                navigator.clipboard.writeText(input.value).then(() => {{
                    alert("Đã sao chép đường dẫn biểu mẫu chính thức!");
                }}).catch(() => {{
                    prompt("Sao chép liên kết:", input.value);
                }});
            }}
        }}

        function saveAdminQrImage() {{
            const container = document.getElementById("adminQrCodeContainer");
            let img = container.querySelector("img");
            if (!img) {{
                const canvas = container.querySelector("canvas");
                if (canvas) {{
                    const a = document.createElement("a");
                    a.href = canvas.toDataURL("image/png");
                    a.download = `QR_${{currentAdminQrFormId}}.png`;
                    a.click();
                    return;
                }}
            }}
            if (img && img.src) {{
                const a = document.createElement("a");
                a.href = img.src;
                a.download = `QR_${{currentAdminQrFormId}}.png`;
                a.click();
            }}
        }}

        document.addEventListener("DOMContentLoaded", () => {{
            const savedView = localStorage.getItem("tb_forms_view");
            if (savedView) setFormViewMode(savedView);
        }});
    </script>
</body>
</html>
"""

def render_form_editor_html(schema: Optional[FormSchema] = None, is_new: bool = False, environment: str = "production") -> str:
    is_dev = (str(environment).lower() != "production")
    if is_dev:
        env_badge = '<span style="display:inline-flex; align-items:center; gap:5px; background:rgba(245,158,11,0.18); color:#f59e0b; border:1px solid rgba(245,158,11,0.45); font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:9999px; vertical-align:middle; margin-left:8px; text-transform:uppercase; letter-spacing:0.5px;">⚡ DEV REPLICA (surveydb_dev)</span>'
    else:
        env_badge = '<span style="display:inline-flex; align-items:center; gap:5px; background:rgba(16,185,129,0.15); color:#10b981; border:1px solid rgba(16,185,129,0.35); font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:9999px; vertical-align:middle; margin-left:8px; text-transform:uppercase; letter-spacing:0.5px;">✓ PRODUCTION</span>'
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
    title_val = html.escape(schema_dict["title"]) if schema else "Biểu Mẫu Mới"
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
            --text: #f8fafc;
            --muted: #94a3b8;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Be Vietnam Pro', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px 16px;
        }}
        .container {{ max-width: 1440px; width: 96%; margin: 0 auto; }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
            margin-bottom: 20px;
        }}
        .tab-nav {{ display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }}
        .tab-btn {{
            background: none;
            border: none;
            color: var(--muted);
            padding: 10px 18px;
            font-size: 0.9rem;
            font-weight: 700;
            cursor: pointer;
            border-bottom: 2px solid transparent;
        }}
        .tab-btn.active {{ color: #38bdf8; border-bottom-color: #38bdf8; }}
        .tab-panel {{ display: none; }}
        .tab-panel.active {{ display: block; }}
        .form-group {{ margin-bottom: 16px; }}
        .form-group label {{ display: block; font-size: 0.82rem; font-weight: 700; color: var(--muted); margin-bottom: 6px; }}
        .form-control {{
            width: 100%;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            color: #fff;
            font-size: 0.9rem;
            outline: none;
        }}
        .btn-save {{
            background: #2563eb;
            color: #fff;
            border: none;
            border-radius: 8px;
            padding: 12px 24px;
            font-weight: 700;
            font-size: 0.95rem;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1 id="txtEditorTitle">{'Tạo Biểu Mẫu Mới' if is_new else 'Chỉnh Sửa: ' + title_val} {env_badge}</h1>
            </div>
            <a href="/admin/forms" style="color:var(--muted); text-decoration:none; font-size:0.85rem; font-weight:600;">← Quay lại danh sách</a>
        </header>

        <div class="tab-nav">
            <button type="button" class="tab-btn active" onclick="switchEditorTab('common', this)">1. Thông tin chung</button>
            <button type="button" class="tab-btn" onclick="switchEditorTab('settings', this)">2. Cài đặt & Phòng chống gian lận</button>
            <button type="button" class="tab-btn" onclick="switchEditorTab('questions', this)">3. Danh sách câu hỏi ({len(schema_dict['questions'])})</button>
        </div>

        <form id="editorForm" onsubmit="return false;">
            <div id="tab-common" class="tab-panel active">
                <div class="form-group">
                    <label>Mã Biểu Mẫu (Form ID)</label>
                    <input type="text" id="formId" class="form-control" value="{html.escape(schema_dict['form_id'])}" {'readonly' if not is_new else ''} required>
                </div>
                <div class="form-group">
                    <label>Tiêu Đề Biểu Mẫu</label>
                    <input type="text" id="formTitle" class="form-control" value="{title_val}" required>
                </div>
                <div class="form-group">
                    <label>Mô Tả Biểu Mẫu</label>
                    <textarea id="formDesc" class="form-control" rows="3">{html.escape(schema_dict['description'])}</textarea>
                </div>
            </div>

            <div id="tab-settings" class="tab-panel">
                <div class="form-group">
                    <label>Giới Hạn Lượt Nộp (0 = Không giới hạn)</label>
                    <input type="number" id="responseLimit" class="form-control" value="{schema_dict['settings']['response_limit']}">
                </div>
                <div class="form-group">
                    <label>Quyền Truy Cập</label>
                    <select id="accessControl" class="form-control">
                        <option value="public" {'selected' if schema_dict['settings']['access_control'] == 'public' else ''}>Công khai (Public)</option>
                        <option value="internal" {'selected' if schema_dict['settings']['access_control'] == 'internal' else ''}>Nội bộ (Internal)</option>
                    </select>
                </div>
            </div>

            <div id="tab-questions" class="tab-panel">
                <div style="background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px;">
                    <div id="questionsBuilderList"></div>
                </div>
            </div>

            <div style="margin-top:24px; padding-top:16px; border-top:1px solid var(--border); display:flex; justify-content:flex-end;">
                <button type="button" class="btn-save" onclick="saveFormSchema()">
                    <svg viewBox="0 0 24 24" style="width:18px; height:18px; fill:currentColor;"><path d="M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/></svg>
                    <span>Lưu Thay Đổi Biểu Mẫu</span>
                </button>
            </div>
        </form>
    </div>

    <script>
        const INITIAL_SCHEMA = {json.dumps(schema_dict, ensure_ascii=False)};
        const questions = INITIAL_SCHEMA.questions || [];
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
            if (!wrap) return;
            wrap.innerHTML = '';
            questions.forEach((q, idx) => {{
                const item = document.createElement('div');
                item.className = 'question-item';
                item.style.padding = '12px';
                item.style.marginBottom = '12px';
                item.style.background = '#090d16';
                item.style.border = '1px solid var(--border)';
                item.style.borderRadius = '8px';
                item.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                        <span style="font-weight:800; font-size:0.92rem; color:#60a5fa;">#${{idx + 1}}. ${{escapeHtml(q.title)}}</span>
                    </div>
                    <div class="field-group" style="margin-bottom:8px;">
                        <label style="font-size:0.75rem; color:var(--muted); font-weight:700;">Tiêu đề câu hỏi</label>
                        <input type="text" class="form-control" value="${{escapeHtml(q.title)}}" oninput="questions[${{idx}}].title = this.value">
                    </div>
                    <div class="field-group">
                        <label style="font-size:0.75rem; color:var(--muted); font-weight:700;">Mô tả / Hướng dẫn</label>
                        <input type="text" class="form-control" value="${{escapeHtml(q.description || '')}}" oninput="questions[${{idx}}].description = this.value">
                    </div>
                    <div class="field-group" style="margin-top:8px;">
                        <label style="font-size:0.75rem; color:var(--muted); font-weight:700;">Các lựa chọn (phân cách bằng dấu phẩy)</label>
                        <input type="text" class="form-control" value="${{escapeHtml((q.options || []).join(', '))}}" oninput="questions[${{idx}}].options = this.value.split(',').map(s=>s.trim()).filter(Boolean)">
                    </div>
                `;
                wrap.appendChild(item);
            }});
        }}
        document.addEventListener('DOMContentLoaded', renderQuestions);
        function switchEditorTab(tabName, btn) {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            const panel = document.getElementById('tab-' + tabName);
            if (panel) panel.classList.add('active');
        }}

        async function saveFormSchema() {{
            const formId = document.getElementById('formId').value.trim();
            const formTitle = document.getElementById('formTitle').value.trim();
            const formDesc = document.getElementById('formDesc').value.trim();
            const responseLimit = parseInt(document.getElementById('responseLimit').value || '0');
            const accessControl = document.getElementById('accessControl').value;

            if (!formId || !formTitle) {{
                alert('Vui lòng điền đủ Mã ID và Tiêu đề biểu mẫu!');
                return;
            }}

            const payload = {{
                form_id: formId,
                title: formTitle,
                description: formDesc,
                settings: {{
                    response_limit: responseLimit,
                    access_control: accessControl,
                    allow_anonymous: false,
                    allow_edit_after_submit: false,
                    is_test_mode: false,
                    image_checks: {{
                        enable_blur_check: true,
                        blur_threshold: 40.0,
                        enable_ai_vision: true,
                        enable_vehicle_match: true,
                        enable_anti_fraud: true,
                        enable_ai_generated_check: true,
                        enable_screen_photo_check: true
                    }}
                }},
                questions: {json.dumps(schema_dict['questions'])}
            }};

            try {{
                const resp = await fetch('/api/v1/forms/upload-schema', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const res = await resp.json();
                if (resp.ok && res.status === 'success') {{
                    alert(`Biểu mẫu '${{formTitle}}' đã được lưu thành công!`);
                    window.location.href = '/admin/forms';
                }} else {{
                    alert('Lỗi lưu: ' + (res.detail || JSON.stringify(res)));
                }}
            }} catch (err) {{
                alert('Lỗi mạng: ' + err);
            }}
        }}
    </script>
</body>
</html>
"""
