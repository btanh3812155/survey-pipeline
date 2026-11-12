# -*- coding: utf-8 -*-
"""
Forms Manager & Visual 3-Tab Editor HTML Rendering Service.
Decoupled into static/ui/forms_manager.html and static/ui/form_editor.html.
"""

import html
import json
import math
import os
from typing import Dict, Optional, List, Any
from core.state import STATIC_DIR
from services.form_engine import FormSchema, FormSettings, FormQuestion, QuestionType


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

    html_file = os.path.join(STATIC_DIR, "ui", "forms_manager.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace('{forms_cards_html}', forms_cards_html)
        content = content.replace('<span id="totalFormsCount">0</span>', f'<span id="totalFormsCount">{total_forms}</span>')
        content = content.replace('<span id="currentPageNum">1</span>', f'<span id="currentPageNum">{current_page}</span>')
        content = content.replace('<span id="totalPagesNum">1</span>', f'<span id="totalPagesNum">{total_pages}</span>')
        return content

    return f"<html><body><div id='formsGrid'>{forms_cards_html}</div></body></html>"


def render_form_editor_html(
    schema: Optional[FormSchema] = None,
    is_new: bool = False,
    environment: str = "production"
) -> str:
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
            "is_test_mode": False,
            "enable_screen_photo_check": True,
            "enable_ai_vision": True,
            "enable_vehicle_match": True,
            "enable_anti_fraud": True,
            "notify_telegram_chat_id": ""
        },
        "questions": []
    }
    title_val = html.escape(schema_dict["title"]) if schema else "Biểu Mẫu Mới"

    html_file = os.path.join(STATIC_DIR, "ui", "form_editor.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()

        schema_json = json.dumps(schema_dict, ensure_ascii=False)
        header_title = "Tạo Biểu Mẫu Mới" if is_new else f"Chỉnh Sửa: {title_val}"

        content = (
            content.replace("{schema_json_str}", schema_json)
            .replace("{'Tạo Biểu Mẫu Mới' if is_new else 'Chỉnh Sửa: ' + (html.escape(schema.title) if schema else '')}", header_title)
            .replace("{title_val}", title_val)
            .replace("{env_badge}", env_badge)
        )
        return content

    return f"<html><body>Form Editor: {title_val}</body></html>"
