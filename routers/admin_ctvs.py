# -*- coding: utf-8 -*-
"""
CTV Directory admin page.

Directory view of `ctv_directory` (codes, contacts, Telegram ids, active flag)
with a compact quota/tag summary per collaborator and inline edit forms.
"""

import html
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from core.state import verify_admin_auth, get_db_pool
from core.ctv import (
    STATIC_CTV_NAMES,
    CTV_CODES,
    get_ctv_directory,
    update_ctv_directory_entry,
)
from core.quotas import get_ctv_quota_evaluations

logger = logging.getLogger("routers.admin_ctvs")
router = APIRouter(tags=["CTV Directory"])


async def _load_directory_rows(db_pool) -> List[Dict[str, Any]]:
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT code, display_name, phone, telegram_user_id, is_active, notes "
                        "FROM ctv_directory ORDER BY code;"
                    )
                    rows = await cur.fetchall()
            if rows:
                return [
                    {
                        "code": row[0],
                        "display_name": row[1],
                        "phone": row[2],
                        "telegram_user_id": row[3],
                        "is_active": row[4],
                        "notes": row[5],
                        "source": "db",
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.warning(f"CTV directory query failed; using cache/static fallback: {e}")

    cached = get_ctv_directory()
    if cached:
        return [
            {**row, "source": "cache"} for row in cached.values()
        ]
    return [
        {
            "code": code,
            "display_name": STATIC_CTV_NAMES.get(code) or code,
            "phone": None,
            "telegram_user_id": None,
            "is_active": True,
            "notes": "static fallback",
            "source": "static",
        }
        for code in CTV_CODES
    ]


def _quota_summary(evaluations: List[Dict[str, Any]]) -> str:
    if not evaluations:
        return '<span style="color:#64748b;">Chưa cấu hình</span>'
    parts = []
    for item in evaluations:
        scope_type = item.get("scope_type") or "global"
        scope_value = item.get("scope_value") or ""
        label = {"form": "biểu mẫu", "tag": "nhãn", "global": "tổng"}.get(scope_type, scope_type)
        scope_text = html.escape(f"{label} {scope_value}".strip())
        maximum = item.get("max_submissions")
        used = item.get("used", 0)
        if maximum is None:
            parts.append(f"{scope_text}: {used}/∞")
        else:
            parts.append(f"{scope_text}: {used}/{maximum}")
    return "<br>".join(parts)


def _render_page(rows: List[Dict[str, Any]]) -> str:
    body_rows = []
    for row in rows:
        code = html.escape(str(row.get("code") or ""))
        name = html.escape(str(row.get("display_name") or ""))
        phone = html.escape(str(row.get("phone") or "—"))
        tg_id = html.escape(str(row.get("telegram_user_id") or "—"))
        active = row.get("is_active", True)
        active_html = (
            '<span style="color:#10b981; font-weight:700;">✔ Active</span>'
            if active
            else '<span style="color:#ef4444; font-weight:700;">✖ Inactive</span>'
        )
        notes = html.escape(str(row.get("notes") or ""))
        quota_html = row.get("quota_summary") or ""
        active_checked = "checked" if active else ""
        body_rows.append(
            f"""
            <tr>
                <td><code>{code}</code></td>
                <td>{name or "—"}</td>
                <td>{phone}</td>
                <td>{tg_id}</td>
                <td>{active_html}</td>
                <td class="quota-cell">{quota_html}</td>
                <td class="notes-cell">{notes}</td>
                <td class="edit-cell">
                    <details>
                        <summary>Sửa</summary>
                        <form method="post" action="/admin/ctvs/{code}">
                            <input name="display_name" value="{name}" placeholder="Tên hiển thị">
                            <input name="phone" value="{'' if phone == '—' else phone}" placeholder="Số điện thoại">
                            <label class="active-toggle"><input type="checkbox" name="is_active" value="1" {active_checked}> Active</label>
                            <button type="submit">Lưu</button>
                        </form>
                    </details>
                </td>
            </tr>
            """
        )
    rows_html = "".join(body_rows)
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Quản Lý CTV - Trust Bridge</title>
    <style>
        :root {{
            --bg: #f8fafc; --card: #ffffff; --text: #0f172a; --muted: #64748b;
            --border: #e2e8f0; --primary: #2563eb;
        }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
        header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 24px; background: var(--card); border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
        h1 {{ margin: 0; font-size: 1.15rem; }}
        p.sub {{ margin: 4px 0 0; color: var(--muted); font-size: 0.85rem; }}
        main {{ padding: 24px; max-width: 1280px; margin: 0 auto; }}
        table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; font-size: 0.88rem; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }}
        th {{ background: #f1f5f9; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }}
        tr:last-child td {{ border-bottom: none; }}
        .quota-cell {{ font-size: 0.8rem; line-height: 1.5; }}
        .notes-cell {{ color: var(--muted); font-size: 0.78rem; max-width: 220px; }}
        details summary {{ cursor: pointer; color: var(--primary); font-weight: 600; font-size: 0.8rem; }}
        .edit-cell form {{ display: grid; gap: 6px; margin-top: 8px; min-width: 180px; }}
        .edit-cell input[type="text"], .edit-cell input:not([type]) {{ width: 100%; padding: 5px 7px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.78rem; }}
        .edit-cell button {{ padding: 5px 10px; border: 0; border-radius: 6px; background: var(--primary); color: #fff; font-weight: 700; font-size: 0.78rem; cursor: pointer; }}
        .active-toggle {{ display: flex; gap: 6px; align-items: center; font-size: 0.78rem; }}
        .empty {{ padding: 40px; text-align: center; color: var(--muted); }}
    </style>
</head>
<body>
    <header>
        <div>
            <h1>Quản Lý Cộng Tác Viên (CTV)</h1>
            <p class="sub">Danh bạ CTV trung tâm: mã, liên hệ, Telegram và mục tiêu. Bấm "Sửa" để cập nhật.</p>
        </div>
    </header>
    <main>
        <table>
            <thead>
                <tr>
                    <th>Mã CTV</th>
                    <th>Tên hiển thị</th>
                    <th>Số điện thoại</th>
                    <th>Telegram ID</th>
                    <th>Trạng thái</th>
                    <th>Mục tiêu (tag/quota)</th>
                    <th>Ghi chú</th>
                    <th>Sửa</th>
                </tr>
            </thead>
            <tbody>
                {rows_html or '<tr><td colspan="8" class="empty">Chưa có CTV nào trong danh bạ.</td></tr>'}
            </tbody>
        </table>
    </main>
</body>
</html>"""


@router.get("/admin/ctvs", response_class=HTMLResponse)
async def admin_ctv_directory(
    admin_user: str = Depends(verify_admin_auth),
):
    """CTV directory + quota summary with inline editing."""
    db_pool = get_db_pool()
    rows = await _load_directory_rows(db_pool)
    for row in rows:
        try:
            row["quota_summary"] = _quota_summary(
                await get_ctv_quota_evaluations(db_pool, row["code"])
            )
        except Exception as e:
            logger.warning(f"Quota summary failed for {row.get('code')}: {e}")
            row["quota_summary"] = '<span style="color:#64748b;">—</span>'
    return HTMLResponse(content=_render_page(rows), status_code=200)


@router.post("/admin/ctvs/{code}")
async def update_admin_ctv(
    code: str,
    display_name: str = Form(""),
    phone: str = Form(""),
    is_active: Optional[str] = Form(None),
    admin_user: str = Depends(verify_admin_auth),
):
    """Updates one CTV directory row (display name, phone, active)."""
    db_pool = get_db_pool()
    if not db_pool:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu chưa sẵn sàng")
    updated = await update_ctv_directory_entry(
        db_pool,
        code,
        display_name=display_name.strip() or None,
        phone=phone.strip() or None,
        is_active=is_active is not None,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy CTV")
    return RedirectResponse(url="/admin/ctvs", status_code=303)
