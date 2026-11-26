# -*- coding: utf-8 -*-
"""
CTV Directory admin page.

Directory view of `ctv_directory` (codes, contacts, Telegram ids, active flag)
with quota/tag summaries and create/edit/remove actions.
"""

import html
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from core.state import verify_admin_auth, get_db_pool
from core.state import FORM_REGISTRY, refresh_form_registry_from_disk
from core.ctv import (
    STATIC_CTV_NAMES,
    CTV_CODES,
    create_ctv_directory_entry,
    delete_ctv_directory_entry,
    get_all_ctv_assignments,
    get_ctv_directory,
    normalize_ctv_code,
    set_ctv_form_assignments,
    update_ctv_directory_entry,
)
from core.quotas import get_ctv_quota_evaluations

logger = logging.getLogger("routers.admin_ctvs")
router = APIRouter(tags=["CTV Directory"])

MESSAGES = {
    "created": ("ok", "Đã thêm CTV mới."),
    "updated": ("ok", "Đã cập nhật CTV."),
    "deleted": ("ok", "Đã xóa CTV khỏi danh bạ. Dữ liệu bài nộp cũ vẫn được giữ."),
    "duplicate": ("bad", "Mã CTV đã tồn tại."),
    "invalid": ("bad", "Mã CTV không hợp lệ. Dùng chữ, số, dấu gạch ngang hoặc gạch dưới."),
    "missing": ("bad", "Không tìm thấy CTV."),
    "database": ("bad", "Cơ sở dữ liệu chưa sẵn sàng."),
}


async def _load_directory_rows(db_pool) -> List[Dict[str, Any]]:
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT c.code, c.display_name, c.phone, c.telegram_user_id,
                               c.is_active, c.notes,
                               COUNT(s.submission_id) AS submission_count,
                               MAX(s.created_at) AS last_submission_at
                        FROM ctv_directory c
                        LEFT JOIN survey_submissions s ON s.ctv_code = c.code
                        GROUP BY c.code, c.display_name, c.phone,
                                 c.telegram_user_id, c.is_active, c.notes
                        ORDER BY c.code;
                        """
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
                        "submission_count": int(row[6] or 0) if len(row) > 6 else 0,
                        "last_submission_at": row[7] if len(row) > 7 else None,
                        "source": "db",
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.warning(f"CTV directory query failed; using cache/static fallback: {e}")

    cached = get_ctv_directory()
    if cached:
        return [{**row, "source": "cache"} for row in cached.values()]
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
        return '<span class="muted">Chưa cấu hình</span>'
    parts = []
    for item in evaluations:
        scope_type = item.get("scope_type") or "global"
        scope_value = item.get("scope_value") or ""
        label = {"form": "form", "tag": "tag", "global": "tổng"}.get(scope_type, scope_type)
        scope_text = html.escape(f"{label} {scope_value}".strip())
        maximum = item.get("max_submissions")
        used = item.get("used", 0)
        if maximum is None:
            parts.append(f'<span class="qr-preview">{scope_text}: {used}/∞</span>')
        else:
            parts.append(f'<span class="qr-preview">{scope_text}: {used}/{maximum}</span>')
    return " ".join(parts)


def _status_badge(active: Any) -> str:
    if active:
        return '<span class="badge on">● Hoạt động</span>'
    return '<span class="badge off">● Đã tắt</span>'


def _page_flash(message: str = "", error: str = "") -> str:
    if message:
        kind = "ok"
        text = message
        if message in MESSAGES:
            kind, text = MESSAGES[message]
        return f'<div class="notice {kind}" role="status">{html.escape(text)}</div>'
    if error:
        return f'<div class="notice bad" role="alert">{html.escape(error)}</div>'
    return ""


def _icon(name: str) -> str:
    paths = {
        "edit": '<path d="m16 3 5 5-12 12H4v-5L16 3Z"/><path d="m14 5 5 5"/>',
        "confirm": '<path d="M5 13l4 4L19 7"/>',
        "delete": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    }
    return f'<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">{paths[name]}</svg>'


def _assignment_editor(code_path: str, assigned_ids: set[str], forms: List[tuple[str, str]]) -> str:
    if not forms:
        return '<span class="muted">Chưa có khảo sát</span>'
    boxes = "".join(
        f'<label style="display:block;margin:4px 0;"><input type="checkbox" name="form_ids" '
        f'value="{html.escape(str(fid), quote=True)}" {"checked" if fid in assigned_ids else ""}> '
        f'{html.escape(str(title))}</label>'
        for fid, title in forms
    )
    count = len(assigned_ids & {fid for fid, _ in forms})
    return (
        f'<details><summary>Phân công ({count}/{len(forms)})</summary>'
        f'<form method="post" action="/admin/ctvs/{code_path}/assignments" style="margin-top:6px;">'
        f'{boxes}<button type="submit" class="secondary" style="margin-top:6px;">Lưu phân công</button>'
        f'</form></details>'
    )


def _render_page(rows: List[Dict[str, Any]], message: str = "", error: str = "", forms=None) -> str:
    active_rows = [row for row in rows if row.get("is_active", True)]
    total_submissions = sum(int(row.get("submission_count") or 0) for row in rows)
    latest = max(
        (row.get("last_submission_at") for row in rows if row.get("last_submission_at")),
        default=None,
    )
    latest_text = latest.strftime("%d/%m/%Y") if hasattr(latest, "strftime") else "—"

    body_rows = []
    for index, row in enumerate(rows):
        code_raw = str(row.get("code") or "")
        code = html.escape(code_raw)
        code_path = html.escape(quote(code_raw, safe=""))
        name_raw = str(row.get("display_name") or "")
        name = html.escape(name_raw)
        phone_raw = str(row.get("phone") or "")
        phone = html.escape(phone_raw or "—")
        tg_id = html.escape(str(row.get("telegram_user_id") or "—"))
        notes_raw = str(row.get("notes") or "")
        notes = html.escape(notes_raw)
        active = bool(row.get("is_active", True))
        count = int(row.get("submission_count") or 0)
        last_seen = row.get("last_submission_at")
        last_seen_text = last_seen.strftime("%d/%m/%Y %H:%M") if hasattr(last_seen, "strftime") else "—"
        quota_html = row.get("quota_summary") or ""
        assign_html = _assignment_editor(code_path, set(row.get("assigned_forms") or ()), forms or [])
        search = html.escape(f"{code_raw} {name_raw} {phone_raw} {notes_raw}".lower(), quote=True)
        phone_val = html.escape(phone_raw, quote=True)
        name_val = html.escape(name_raw, quote=True)
        notes_val = html.escape(notes_raw, quote=True)
        active_checked = "checked" if active else ""
        form_id = f"ctv-edit-{index}"
        body_rows.append(
            f"""
            <tr data-ctv-row data-search="{search}" data-active="{'1' if active else '0'}">
              <td>
                <form id="{form_id}" method="post" action="/admin/ctvs/{code_path}"></form>
                <form id="ctv-delete-{index}" method="post" action="/admin/ctvs/{code_path}/delete" data-delete-code="{code}"></form>
                <details class="ctv-details" id="ctv-details-{index}">
                  <summary><code>{code}</code></summary>
                  <div class="ctv-expanded">
                    <dl>
                      <dt>Tên hiển thị</dt><dd><span class="ctv-view">{name or '—'}</span><input class="ctv-edit" form="{form_id}" name="display_name" value="{name_val}" maxlength="120" placeholder="Tên CTV" disabled></dd>
                      <dt>Ghi chú</dt><dd><span class="ctv-view">{notes or '—'}</span><input class="ctv-edit" form="{form_id}" name="notes" value="{notes_val}" maxlength="500" placeholder="Ghi chú nội bộ" disabled></dd>
                      <dt>Liên hệ</dt><dd><span class="ctv-view">{phone}</span><input class="ctv-edit" form="{form_id}" name="phone" value="{phone_val}" maxlength="32" inputmode="tel" placeholder="09xx xxx xxx" disabled></dd>
                      <dt>Telegram</dt><dd>{tg_id}</dd>
                      <dt>Trạng thái</dt><dd><span class="ctv-view">{_status_badge(active)}</span><label class="ctv-edit inline-active"><input form="{form_id}" type="checkbox" name="is_active" value="1" {active_checked} disabled> Hoạt động</label></dd>
                      <dt>Bài nộp</dt><dd>{count} · {last_seen_text}</dd>
                      <dt>Mục tiêu</dt><dd>{quota_html}</dd>
                      <dt>Khảo sát</dt><dd>{assign_html}</dd>
                    </dl>
                    <div class="row-actions">
                      <button type="button" class="secondary icon-button" data-edit-ctv="mobile" aria-label="Sửa CTV {code}" title="Sửa CTV">{_icon('edit')}</button>
                      <button type="submit" form="{form_id}" class="confirm icon-button" data-confirm-ctv aria-label="Lưu CTV {code}" title="Lưu thay đổi" hidden>{_icon('confirm')}</button>
                      <button type="submit" form="ctv-delete-{index}" class="danger icon-button" aria-label="Xóa CTV {code}" title="Xóa CTV">{_icon('delete')}</button>
                    </div>
                  </div>
                </details>
                <div class="ctv-inline-meta">
                  <input class="ctv-edit" form="{form_id}" name="display_name" value="{name_val}" maxlength="120" placeholder="Tên hiển thị" aria-label="Tên hiển thị CTV {code}" disabled>
                  <input class="ctv-edit" form="{form_id}" name="notes" value="{notes_val}" maxlength="500" placeholder="Ghi chú" aria-label="Ghi chú CTV {code}" disabled>
                </div>
              </td>
              <td class="ctv-desktop">
                <div class="ctv-view">{phone}</div>
                <input class="ctv-edit" form="{form_id}" name="phone" value="{phone_val}" maxlength="32" inputmode="tel" placeholder="09xx xxx xxx" aria-label="Số điện thoại CTV {code}" disabled>
                <div class="muted">Telegram: {tg_id}</div>
              </td>
              <td class="ctv-desktop">
                <span class="ctv-view">{_status_badge(active)}</span>
                <label class="ctv-edit inline-active"><input form="{form_id}" type="checkbox" name="is_active" value="1" {active_checked} disabled> Hoạt động</label>
              </td>
              <td class="ctv-desktop"><strong>{count}</strong><div class="muted">{last_seen_text}</div></td>
              <td class="ctv-desktop ctv-quota">{quota_html}<div style="margin-top:6px;">{assign_html}</div></td>
              <td class="ctv-desktop">
                <div class="row-actions">
                  <button type="button" class="secondary icon-button" data-edit-ctv="desktop" aria-label="Sửa CTV {code}" title="Sửa CTV">{_icon('edit')}</button>
                  <button type="submit" form="{form_id}" class="confirm icon-button" data-confirm-ctv aria-label="Lưu CTV {code}" title="Lưu thay đổi" hidden>{_icon('confirm')}</button>
                  <button type="submit" form="ctv-delete-{index}" class="danger icon-button" aria-label="Xóa CTV {code}" title="Xóa CTV">{_icon('delete')}</button>
                </div>
              </td>
            </tr>
            """
        )

    ctv_options = ''.join(
        f'<option value="{html.escape(str(row["code"]), quote=True)}">{html.escape(str(row["code"]))}</option>'
        for row in active_rows if row.get("source") == "db"
    )
    form_options = ''.join(
        f'<option value="{html.escape(str(form_id), quote=True)}">{html.escape(str(title))}</option>'
        for form_id, title in (forms or [])
    )

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quản Lý CTV - Trust Bridge</title>
<link rel="stylesheet" href="/static/ui/admin_ctvs.css">
<script src="/static/qrcode.min.js" defer></script>
<script src="/static/ui/admin_ctvs.js" defer></script>
</head>
<body>
<main>
  <header class="admin-hero">
    <div>
      <span class="eyebrow">CTV Directory</span>
      <h1>Quản Lý CTV</h1>
      <p class="sub">Danh bạ cộng tác viên, liên hệ, Telegram, mục tiêu và số bài nộp. Thêm, sửa, tạm tắt hoặc xóa CTV ngay tại đây.</p>
    </div>
    <div class="hero-actions">
      <a class="button secondary" href="#new-ctv">+ Thêm CTV</a>
    </div>
  </header>

  {_page_flash(message, error)}

  <section class="stats-grid" aria-label="Tổng quan CTV">
    <div class="stat-card total">
      <div class="stat-title">Tổng CTV</div>
      <div class="stat-value">{len(rows)}</div>
    </div>
    <div class="stat-card approved">
      <div class="stat-title">Đang hoạt động</div>
      <div class="stat-value">{len(active_rows)}</div>
    </div>
    <div class="stat-card review">
      <div class="stat-title">Bài nộp liên kết</div>
      <div class="stat-value">{total_submissions}</div>
    </div>
    <div class="stat-card rejected">
      <div class="stat-title">Hoạt động gần nhất</div>
      <div class="stat-value" style="font-size:1rem;">{latest_text}</div>
    </div>
  </section>

  <div class="admin-stack">
    <section class="card ctv-generator" aria-labelledby="ctv-link-title">
      <h2 id="ctv-link-title">Tạo liên kết và mã QR CTV</h2>
      <form id="ctv-link-form">
        <label>CTV đang hoạt động<select id="ctv-link-code" data-searchable-select required><option value="">Chọn CTV</option>{ctv_options}</select></label>
        <label>Biểu mẫu công khai đang mở<select id="ctv-link-form-id" data-searchable-select required><option value="">Chọn biểu mẫu</option>{form_options}</select></label>
        <button type="submit">Tạo liên kết / QR</button>
      </form>
      <p class="muted">Chỉ liệt kê CTV đang hoạt động được xác nhận từ cơ sở dữ liệu và biểu mẫu công khai, còn nhận phản hồi, bật ghi nhận CTV. Tải lại trang để cập nhật danh sách.</p>
      <div id="ctv-link-output" hidden>
        <label>Liên kết<input id="ctv-generated-link" readonly></label>
        <div class="row-actions"><button type="button" id="ctv-copy-link" disabled>Sao chép liên kết</button><button type="button" id="ctv-share-link" disabled>Chia sẻ liên kết</button></div>
        <div id="ctv-qr-preview"></div>
        <div class="row-actions"><button type="button" id="ctv-copy-image" disabled>Sao chép ảnh QR</button><button type="button" id="ctv-share-image" disabled>Chia sẻ ảnh QR</button><button type="button" id="ctv-download-image" disabled>Tải PNG</button></div>
      </div>
      <p id="ctv-link-status" role="status" aria-live="polite"></p>
    </section>

    <details class="card" id="new-ctv">
      <summary><strong>Thêm CTV</strong> <span class="muted">Tạo danh bạ mới</span></summary>
      <div style="margin-top:14px;">
        <form method="post" action="/admin/ctvs" class="inline-form">
          <label>Mã CTV<input name="code" required maxlength="64" placeholder="VD: TUAN_LM_HCM_001" autocomplete="off"></label>
          <label>Tên hiển thị<input name="display_name" maxlength="120" placeholder="Tên CTV"></label>
          <label>Số điện thoại<input name="phone" maxlength="32" inputmode="tel" placeholder="09xx xxx xxx"></label>
          <label>Ghi chú<input name="notes" maxlength="500" placeholder="Ghi chú nội bộ"></label>
          <label><span><input type="checkbox" name="is_active" value="1" checked> Kích hoạt ngay</span></label>
          <div class="actions"><button type="submit">Thêm CTV</button></div>
        </form>
        <div style="margin-top:14px; padding-top:12px; border-top:1px solid var(--border);">
          <div class="panel-title"><h2>Quy ước</h2></div>
          <p class="muted">Mã CTV được chuẩn hóa thành chữ in hoa, không dấu, dùng gạch dưới. Xóa CTV chỉ xóa danh bạ, liên kết form và quota; bài nộp lịch sử vẫn giữ nguyên.</p>
          <p class="muted">Telegram ID chỉ được điền khi CTV liên kết bot, không sửa trực tiếp tại đây.</p>
        </div>
      </div>
    </details>

    <div class="admin-stack">
      <form class="admin-toolbar" method="get" action="/admin/ctvs" onsubmit="return false;">
        <label class="search" aria-label="Tìm CTV">
          <input type="search" id="ctvSearch" placeholder="Tìm theo mã, tên, số điện thoại hoặc ghi chú..." oninput="filterCtvRows()">
        </label>
        <div class="filter-pills" role="group" aria-label="Lọc trạng thái">
          <button type="button" class="active" data-status-filter="all" onclick="setCtvStatus(this)">Tất cả</button>
          <button type="button" data-status-filter="active" onclick="setCtvStatus(this)">Hoạt động</button>
          <button type="button" data-status-filter="inactive" onclick="setCtvStatus(this)">Đã tắt</button>
        </div>
      </form>

      <section class="card table-card">
        <table>
          <thead>
            <tr>
              <th>Mã CTV</th>
              <th>Liên hệ</th>
              <th>Trạng thái</th>
              <th>Bài nộp</th>
              <th>Mục tiêu</th>
              <th>Thao tác</th>
            </tr>
          </thead>
          <tbody id="ctvTableBody" data-tb-paginate="tr[data-ctv-row]" data-page-size="15">
            {''.join(body_rows) or '<tr><td colspan="6" class="empty">Chưa có CTV nào trong danh bạ.</td></tr>'}
          </tbody>
        </table>
      </section>
    </div>
  </div>
</main>
<script>
function filterCtvRows() {{
  const q = (document.getElementById('ctvSearch').value || '').trim().toLowerCase();
  const status = document.querySelector('[data-status-filter].active')?.dataset.statusFilter || 'all';
  const body = document.getElementById('ctvTableBody');
  document.querySelectorAll('[data-ctv-row]').forEach((row) => {{
    const matchesText = !q || (row.dataset.search || '').includes(q);
    const matchesStatus = status === 'all'
      || (status === 'active' && row.dataset.active === '1')
      || (status === 'inactive' && row.dataset.active === '0');
    const ok = matchesText && matchesStatus;
    row.dataset.filterHidden = ok ? '0' : '1';
    if (!body?._tbPaginateRender) row.hidden = !ok;
  }});
  if (body?._tbPaginateRender) body._tbPaginateRender();
}}
function setCtvStatus(button) {{
  document.querySelectorAll('[data-status-filter]').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  filterCtvRows();
}}
</script>
</body>
</html>"""


@router.get("/admin/ctvs", response_class=HTMLResponse)
async def admin_ctv_directory(
    msg: str = "",
    err: str = "",
    admin_user: str = Depends(verify_admin_auth),
):
    """CTV directory with create/edit/toggle/delete actions."""
    db_pool = get_db_pool()
    rows = await _load_directory_rows(db_pool)
    assignments_by_ctv = await get_all_ctv_assignments(db_pool)
    for row in rows:
        row["assigned_forms"] = assignments_by_ctv.get(str(row.get("code") or ""), set())
        try:
            row["quota_summary"] = _quota_summary(
                await get_ctv_quota_evaluations(db_pool, row["code"])
            )
        except Exception as e:
            logger.warning(f"Quota summary failed for {row.get('code')}: {e}")
            row["quota_summary"] = '<span class="muted">—</span>'
    refresh_form_registry_from_disk()
    forms = [
        (form_id, schema.title)
        for form_id, schema in sorted(FORM_REGISTRY.items())
        if schema.settings.access_control == "public"
        and not (schema.settings.response_limit and schema.settings.current_responses >= schema.settings.response_limit)
    ]
    return HTMLResponse(content=_render_page(rows, message=msg, error=err, forms=forms), status_code=200)


@router.post("/admin/ctvs")
async def create_admin_ctv(
    code: str = Form(...),
    display_name: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    is_active: Optional[str] = Form(None),
    admin_user: str = Depends(verify_admin_auth),
):
    """Creates a new CTV directory entry."""
    db_pool = get_db_pool()
    if not db_pool:
        return RedirectResponse(url="/admin/ctvs?msg=database", status_code=303)
    normalized = normalize_ctv_code(code)
    if not normalized:
        return RedirectResponse(url="/admin/ctvs?msg=invalid", status_code=303)
    created = await create_ctv_directory_entry(
        db_pool,
        normalized,
        display_name=display_name.strip() or None,
        phone=phone.strip() or None,
        notes=notes.strip() or None,
        is_active=is_active is not None,
    )
    if created:
        from services.audit_and_privacy import record_audit_event
        await record_audit_event(db_pool, "ADMIN_CTV", "CTV_CREATED", "SYSTEM", normalized, "ADMIN", admin_user, {"is_active": is_active is not None})
    return RedirectResponse(
        url=f"/admin/ctvs?msg={'created' if created else 'duplicate'}",
        status_code=303,
    )


@router.post("/admin/ctvs/{code}")
async def update_admin_ctv(
    code: str,
    display_name: str = Form(""),
    phone: str = Form(""),
    notes: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    admin_user: str = Depends(verify_admin_auth),
):
    """Updates one CTV directory row (display name, phone, notes, active)."""
    db_pool = get_db_pool()
    if not db_pool:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu chưa sẵn sàng")
    updated = await update_ctv_directory_entry(
        db_pool,
        code,
        display_name=display_name.strip() or None,
        phone=phone.strip() or None,
        notes=notes if notes is not None else None,
        is_active=is_active is not None,
    )
    if not updated:
        return RedirectResponse(url="/admin/ctvs?msg=missing", status_code=303)
    from services.audit_and_privacy import record_audit_event
    await record_audit_event(db_pool, "ADMIN_CTV", "CTV_UPDATED", "SYSTEM", code, "ADMIN", admin_user, {"is_active": is_active is not None})
    return RedirectResponse(url="/admin/ctvs?msg=updated", status_code=303)


@router.post("/admin/ctvs/{code}/toggle")
async def toggle_admin_ctv(
    code: str,
    display_name: str = Form(""),
    phone: str = Form(""),
    notes: Optional[str] = Form(None),
    admin_user: str = Depends(verify_admin_auth),
):
    """Flips a CTV between active and inactive without opening the edit form."""
    db_pool = get_db_pool()
    if not db_pool:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu chưa sẵn sàng")
    rows = await _load_directory_rows(db_pool)
    current = next((row for row in rows if str(row.get("code")) == str(code)), None)
    if not current:
        return RedirectResponse(url="/admin/ctvs?msg=missing", status_code=303)
    await update_ctv_directory_entry(
        db_pool,
        code,
        display_name=display_name.strip() or current.get("display_name"),
        phone=phone.strip() or current.get("phone"),
        notes=notes if notes is not None else current.get("notes"),
        is_active=not bool(current.get("is_active", True)),
    )
    return RedirectResponse(url="/admin/ctvs?msg=updated", status_code=303)


@router.post("/admin/ctvs/{code}/assignments")
async def update_admin_ctv_assignments(
    code: str,
    form_ids: List[str] = Form(default=[]),
    admin_user: str = Depends(verify_admin_auth),
):
    """Replaces the assigned surveys for a single CTV."""
    db_pool = get_db_pool()
    if not db_pool:
        return RedirectResponse(url="/admin/ctvs?msg=database", status_code=303)
    updated = await set_ctv_form_assignments(db_pool, code, form_ids, assigned_by=admin_user)
    if updated is None:
        return RedirectResponse(url="/admin/ctvs?msg=missing", status_code=303)
    from services.audit_and_privacy import record_audit_event
    await record_audit_event(db_pool, "ADMIN_CTV", "CTV_ASSIGNMENTS_UPDATED", "SYSTEM", code, "ADMIN", admin_user, {"form_ids": updated})
    return RedirectResponse(url="/admin/ctvs?msg=updated", status_code=303)


@router.post("/admin/ctvs/{code}/delete")
async def delete_admin_ctv(
    code: str,
    admin_user: str = Depends(verify_admin_auth),
):
    """Removes a CTV directory entry while preserving historical submissions."""
    db_pool = get_db_pool()
    if not db_pool:
        return RedirectResponse(url="/admin/ctvs?msg=database", status_code=303)
    deleted = await delete_ctv_directory_entry(db_pool, code)
    if deleted:
        from services.audit_and_privacy import record_audit_event
        await record_audit_event(db_pool, "ADMIN_CTV", "CTV_DELETED", "SYSTEM", code, "ADMIN", admin_user, {})
    return RedirectResponse(
        url=f"/admin/ctvs?msg={'deleted' if deleted else 'missing'}",
        status_code=303,
    )
