# -*- coding: utf-8 -*-
"""
Dynamic Form & CTV Landing HTML Rendering Service.
Decoupled into static/ui/dynamic_form.html and static/ui/ctv_landing.html.
"""

import html
import os
from typing import Optional, List
from config import settings
from core.state import STATIC_DIR
from services.form_engine import FormSchema


def render_dynamic_form_html(
    schema: FormSchema,
    telegram_bot_username: Optional[str] = None,
    ctv_code: Optional[str] = None,
    ctv_slug: Optional[str] = None,
    ref_code: Optional[str] = None,
) -> str:
    tg_bot = telegram_bot_username if telegram_bot_username is not None else (getattr(settings, "TELEGRAM_DRIVER_BOT_USERNAME", None) or settings.TELEGRAM_BOT_USERNAME or "TrustBridgeVN_bot")
    locked_ctv = ctv_code or ""
    active_ctv_slug = ctv_slug or ""
    active_ref = ref_code or (ctv_code.split("_")[0].lower() if ctv_code and ctv_code.lower().startswith("ctv") else "")
    clean_title = (schema.title or "").replace("Trust Bridge - ", "").replace("Trust Bridge – ", "").strip()
    escaped_title = html.escape(clean_title)
    escaped_desc = html.escape(schema.description or "")

    is_form_test_mode = bool(schema.settings and schema.settings.is_test_mode)
    test_banner_html = ""
    if is_form_test_mode:
        test_banner_html = """
    <!-- Test Mode Banner with Bypass Toggle -->
    <div class="test-banner" id="testModeBanner">
        <div style="display:flex; flex-direction:column; gap:4px;">
            <span><span id="txtModeLabel">Chế độ biểu mẫu:</span> <b id="testModeStatus">Thử nghiệm</b></span>
            <label id="bypassCheckLabel" style="display:flex; align-items:center; gap:6px; font-size:0.75rem; color:#fde68a; cursor:pointer;">
                <input type="checkbox" id="bypassImgToggle" onchange="toggleBypassImg(this.checked)">
                <span id="txtBypassLabel">Bỏ qua kiểm tra ảnh (Test nhanh dữ liệu)</span>
            </label>
        </div>
        <button type="button" class="btn-test-toggle" id="btnToggleTestMode" onclick="toggleTestMode()">Thử nghiệm</button>
    </div>
        """

    schema_json = schema.model_dump_json().replace("</script>", "<\\/script>")

    html_file = os.path.join(STATIC_DIR, "ui", "dynamic_form.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()

        content = (
            content.replace("{escaped_title}", escaped_title)
            .replace("{escaped_desc}", escaped_desc)
            .replace("{test_banner_html}", test_banner_html)
            .replace("{schema_json}", schema_json)
            .replace("{locked_ctv}", locked_ctv)
            .replace("{active_ctv_slug}", active_ctv_slug)
            .replace("{active_ref}", active_ref)
            .replace("{tg_bot}", tg_bot)
            .replace("{'flex' if tg_bot else 'none'}", "flex" if tg_bot else "none")
        )
        return content

    return f"<html><body>{escaped_title}</body></html>"


def render_ctv_selector_landing_html(
    schema: FormSchema,
    ctv_options: List[str],
    form_id: str = "income_survey",
    query_params: Optional[dict] = None,
) -> str:
    """Renders clean mobile-first CTV selection landing page when accessed without ?ref=."""
    clean_title = (schema.title or "Biểu Mẫu Khảo Sát").replace("Trust Bridge - ", "").replace("Trust Bridge – ", "").strip()
    escaped_title = html.escape(clean_title)
    escaped_desc = html.escape(schema.description or "Khảo sát và xác thực thông tin đối soát")

    from core.ctv import normalize_ctv_code

    options_html = '<option value="">-- Vui lòng chọn CTV phụ trách --</option>'
    for opt in ctv_options:
        code = normalize_ctv_code(str(opt))
        if not code:
            continue
        options_html += f'<option value="{html.escape(code)}">{html.escape(code)}</option>'

    html_file = os.path.join(STATIC_DIR, "ui", "ctv_landing.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()

        content = (
            content.replace("{escaped_title}", escaped_title)
            .replace("{escaped_desc}", escaped_desc)
            .replace("{options_html}", options_html)
            .replace("{form_id}", form_id)
        )
        return content

    return f"<html><body>{escaped_title} - Chọn CTV</body></html>"
