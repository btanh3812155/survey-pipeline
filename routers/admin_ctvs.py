# -*- coding: utf-8 -*-
"""
CTV Directory admin page.

Read-only view of `ctv_directory` (codes, contacts, Telegram ids, active flag)
with a compact quota/tag summary per collaborator. Inline editing lives in the
database for now; the page just surfaces the platform layer for operators.
"""

import html
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from core.state import verify_admin_auth, get_db_pool
from core.ctv import (
    STATIC_CTV_NAMES,
    CTV_CODES,
    get_ctv_codes,
    get_ctv_directory,
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
        .nav-actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .nav-btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--card); color: var(--text); text-decoration: none; font-size: 0.82rem; font-weight: 600; }}
        .nav-btn.primary {{ background: var(--primary); border-color: var(--primary); color: #fff; }}
        main {{ padding: 24px; max-width: 1280px; margin: 0 auto; }}
        table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; font-size: 0.88rem; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }}
        th {{ background: #f1f5f9; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }}
        tr:last-child td {{ border-bottom: none; }}
        .quota-cell {{ font-size: 0.8rem; line-height: 1.5; }}
        .notes-cell {{ color: var(--muted); font-size: 0.78rem; max-width: 220px; }}
        .empty {{ padding: 40px; text-align: center; color: var(--muted); }}
    </style>
</head>
<body>
    <header>
        <div>
            <h1>Quản Lý Cộng Tác Viên (CTV)</h1>
            <p class="sub">Danh bạ CTV trung tâm: mã, liên hệ, Telegram và hạn mức.</p>
        </div>
        <div class="nav-actions">
            <a href="/admin/forms" class="nav-btn">Quản Lý Form</a>
            <a href="/admin/submissions" class="nav-btn">Dữ Liệu Nộp</a>
            <a href="/admin/audit" class="nav-btn">Nhật Ký Kiểm Toán</a>
            <a href="/admin/media" class="nav-btn">Quản Lý Media</a>
            <a href="/admin/ctvs" class="nav-btn primary">CTV</a>
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
                    <th>Hạn mức (tag/quota)</th>
                    <th>Ghi chú</th>
                </tr>
            </thead>
            <tbody>
                {rows_html or '<tr><td colspan="7" class="empty">Chưa có CTV nào trong danh bạ.</td></tr>'}
            </tbody>
        </table>
    </main>
</body>
</html>"""


@router.get("/admin/ctvs", response_class=HTMLResponse)
async def admin_ctv_directory(
    admin_user: str = Depends(verify_admin_auth),
):
    """Read-only CTV directory + quota summary."""
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
