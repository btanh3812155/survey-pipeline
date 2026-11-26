# -*- coding: utf-8 -*-
"""
Admin Appeals Moderation Router.

Displays submissions in `APPEAL_PENDING` status and allows authorized staff
(superadmin, manager, reviewer) to approve or reject appeals through the
existing durable transition pipeline with full audit logging.
"""

import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from core.durable_work import transition_submission
from core.state import FORM_REGISTRY, get_db_pool, refresh_form_registry_from_disk, verify_admin_auth
from core.submission_ids import normalize_submission_id
from services.audit_and_privacy import record_audit_event
from services.submission_media import manifest_paths

logger = logging.getLogger("routers.admin_appeals")
router = APIRouter(tags=["Admin Appeals"])

MESSAGES = {
    "approved": ("ok", "Đã duyệt phúc khảo và cập nhật trạng thái phiếu."),
    "rejected": ("ok", "Đã từ chối phúc khảo và cập nhật trạng thái phiếu."),
}

ERRORS = {
    "not_found": "Không tìm thấy phiếu khảo sát.",
    "not_pending": "Phiếu này không còn ở trạng thái chờ phúc khảo.",
    "database": "Cơ sở dữ liệu chưa sẵn sàng.",
    "unavailable": "Không thể xử lý phúc khảo lúc này. Vui lòng thử lại.",
    "invalid": "Mã phiếu không hợp lệ.",
}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> Optional[List[Any]]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else None
        except Exception:
            return None
    return value if isinstance(value, list) else None


async def _load_pending_appeals(db_pool, query: str = "") -> List[Dict[str, Any]]:
    if not db_pool:
        return []
    q = str(query or "").strip()
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                if q:
                    like = f"%{q}%"
                    await cur.execute(
                        """
                        SELECT submission_id, form_id, ctv_code, collaborator_id,
                               driver_phone, driver_name, rejection_reason,
                               created_at, moderated_at, form_data,
                               to_jsonb(s)->'media_manifest' AS media_manifest
                        FROM survey_submissions s
                        WHERE status = 'APPEAL_PENDING'
                          AND (
                              submission_id ILIKE %s
                              OR COALESCE(form_id, '') ILIKE %s
                              OR COALESCE(ctv_code, '') ILIKE %s
                              OR COALESCE(driver_phone, '') ILIKE %s
                              OR COALESCE(driver_name, '') ILIKE %s
                              OR COALESCE(rejection_reason, '') ILIKE %s
                          )
                        ORDER BY COALESCE(moderated_at, created_at) DESC, created_at DESC
                        LIMIT 500;
                        """,
                        (like, like, like, like, like, like),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT submission_id, form_id, ctv_code, collaborator_id,
                               driver_phone, driver_name, rejection_reason,
                               created_at, moderated_at, form_data,
                               to_jsonb(s)->'media_manifest' AS media_manifest
                        FROM survey_submissions s
                        WHERE status = 'APPEAL_PENDING'
                        ORDER BY COALESCE(moderated_at, created_at) DESC, created_at DESC
                        LIMIT 500;
                        """
                    )
                rows = await cur.fetchall()
    except Exception as exc:
        logger.warning("Failed to query pending appeals: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    for row in rows:
        sub_id = str(row[0] or "")
        form_data = _as_dict(row[9])
        manifest = _as_list(row[10])
        ctv_val = row[2] or form_data.get("ctv_code") or row[3] or form_data.get("collaborator_id") or ""
        photos = [
            f"/admin/submissions/{quote(sub_id, safe='')}/photos/{quote(os.path.basename(p), safe='')}"
            for p in manifest_paths(sub_id, manifest)
        ]
        results.append(
            {
                "submission_id": sub_id,
                "form_id": str(row[1] or ""),
                "ctv_code": str(ctv_val or ""),
                "driver_phone": str(row[4] or ""),
                "driver_name": str(row[5] or ""),
                "appeal_reason": str(row[6] or ""),
                "created_at": row[7],
                "moderated_at": row[8],
                "photo_urls": photos,
            }
        )
    return results


def _fmt_ts(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y %H:%M")
    return str(value) if value else "—"


def _page_flash(msg: str = "", err: str = "") -> str:
    if msg and msg in MESSAGES:
        kind, text = MESSAGES[msg]
        return f'<div class="notice {kind}" role="status">{html.escape(text)}</div>'
    if err:
        text = ERRORS.get(err, err)
        return f'<div class="notice bad" role="alert">{html.escape(text)}</div>'
    return ""


def _render_appeals_page(
    rows: List[Dict[str, Any]],
    *,
    query: str = "",
    msg: str = "",
    err: str = "",
    can_moderate: bool = True,
) -> str:
    body_rows: List[str] = []
    for index, row in enumerate(rows):
        sub_raw = row["submission_id"]
        sub_esc = html.escape(sub_raw)
        sub_path = html.escape(quote(sub_raw, safe=""), quote=True)
        form_id_raw = row["form_id"]
        schema = FORM_REGISTRY.get(form_id_raw)
        form_title = schema.title if schema else ""
        form_cell = f"<code>{html.escape(form_id_raw or '—')}</code>"
        if form_title:
            form_cell += f'<div class="muted">{html.escape(form_title)}</div>'

        ctv_raw = row["ctv_code"]
        ctv_cell = f"<code>{html.escape(ctv_raw)}</code>" if ctv_raw else '<span class="muted">—</span>'

        driver_name = html.escape(row["driver_name"] or "—")
        driver_phone = html.escape(row["driver_phone"] or "—")
        reason_esc = html.escape(row["appeal_reason"] or "—")
        created_text = html.escape(_fmt_ts(row["created_at"]))
        updated_text = html.escape(_fmt_ts(row["moderated_at"]))

        details_href = f"/admin/submissions?search={quote(sub_raw, safe='')}"
        links_parts = [f'<a href="{details_href}">Xem hồ sơ</a>']
        if row["photo_urls"]:
            for p_idx, p_url in enumerate(row["photo_urls"], start=1):
                links_parts.append(
                    f'<a href="{html.escape(p_url, quote=True)}" target="_blank" rel="noopener">Ảnh {p_idx}</a>'
                )
        else:
            links_parts.append(
                f'<a href="/api/v1/submissions/{sub_path}/photos" target="_blank" rel="noopener">Danh sách ảnh</a>'
            )
        links_html = '<div class="muted" style="margin-top:4px;display:flex;gap:8px;flex-wrap:wrap;">' + "".join(links_parts) + "</div>"

        if can_moderate:
            action_cell = (
                f'<form method="post" action="/admin/appeals/{sub_path}/approve" class="appeal-action-form">'
                f'<input type="text" name="notes" maxlength="500" placeholder="Ghi chú xét duyệt (tùy chọn)..." '
                f'aria-label="Ghi chú xét duyệt cho phiếu {sub_esc}">'
                f'<div class="actions" style="margin-top:8px;">'
                f'<button type="submit">Duyệt phúc khảo</button>'
                f'<button type="submit" formaction="/admin/appeals/{sub_path}/reject" class="danger">Từ chối phúc khảo</button>'
                f'</div>'
                f'</form>'
            )
        else:
            action_cell = '<span class="muted">Chỉ xem</span>'

        search_blob = html.escape(
            f"{sub_raw} {form_id_raw} {form_title} {ctv_raw} {row['driver_name']} {row['driver_phone']} {row['appeal_reason']}".lower(),
            quote=True,
        )
        body_rows.append(
            f"""
            <tr data-appeal-row data-search="{search_blob}">
              <td>
                <code>{sub_esc}</code>
                <div><span class="status review" style="margin-top:4px;">APPEAL_PENDING</span></div>
                {links_html}
              </td>
              <td>{form_cell}</td>
              <td>{ctv_cell}</td>
              <td>
                <div><strong>{driver_name}</strong></div>
                <div class="muted">{driver_phone}</div>
              </td>
              <td style="max-width:300px;white-space:normal;word-break:break-word;">{reason_esc}</td>
              <td>
                <div><span class="muted">Nộp:</span> {created_text}</div>
                <div><span class="muted">Phúc khảo:</span> {updated_text}</div>
              </td>
              <td style="min-width:250px;">{action_cell}</td>
            </tr>
            """
        )

    table_body = "".join(body_rows) or (
        '<tr><td colspan="7" class="empty" style="text-align:center;padding:28px;">'
        'Hiện không có yêu cầu phúc khảo nào đang chờ xử lý.'
        '</td></tr>'
    )
    query_esc = html.escape(query, quote=True)

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Xét Duyệt Phúc Khảo - Trust Bridge</title>
<style>
  .appeal-toolbar {{
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }}
  .appeal-toolbar input[type="search"] {{
    flex: 1 1 280px;
    margin-top: 0;
  }}
  .appeal-action-form input[type="text"] {{
    margin-top: 0;
    font-size: 0.82rem !important;
    min-height: 34px;
  }}
</style>
</head>
<body>
<main>
  <header class="admin-hero">
    <div>
      <h1>Xét Duyệt Phúc Khảo</h1>
      <p class="sub">Danh sách phiếu đang chờ phúc khảo (APPEAL_PENDING). Duyệt hoặc từ chối kèm ghi chú đối soát.</p>
    </div>
    <div class="hero-actions">
      <a class="button secondary" href="/admin/submissions">Dữ liệu nộp</a>
      <a class="button secondary" href="/admin/appeals">Làm Mới</a>
    </div>
  </header>

  {_page_flash(msg, err)}

  <section class="stats-grid" aria-label="Tổng quan phúc khảo">
    <div class="stat-card review">
      <div class="stat-title">Đang chờ phúc khảo</div>
      <div class="stat-value">{len(rows)}</div>
    </div>
  </section>

  <form class="appeal-toolbar" method="get" action="/admin/appeals">
    <input type="search" id="appealSearch" name="q" value="{query_esc}" placeholder="Tìm theo mã phiếu, form, CTV, SĐT, tên tài xế hoặc lý do..." oninput="filterAppealRows()">
    <button type="submit" class="secondary">Tìm kiếm</button>
  </form>

  <section class="card table-card">
    <table>
      <thead>
        <tr>
          <th>Mã phiếu</th>
          <th>Biểu mẫu</th>
          <th>CTV</th>
          <th>Tài xế</th>
          <th>Lý do phúc khảo</th>
          <th>Thời gian</th>
          <th>Thao tác</th>
        </tr>
      </thead>
      <tbody id="appealsTableBody" data-tb-paginate="tr[data-appeal-row]" data-page-size="15">
        {table_body}
      </tbody>
    </table>
  </section>
</main>
<script>
function filterAppealRows() {{
  const q = (document.getElementById('appealSearch').value || '').trim().toLowerCase();
  const body = document.getElementById('appealsTableBody');
  document.querySelectorAll('[data-appeal-row]').forEach((row) => {{
    const ok = !q || (row.dataset.search || '').includes(q);
    row.dataset.filterHidden = ok ? '0' : '1';
    if (!body?._tbPaginateRender) row.hidden = !ok;
  }});
  if (body?._tbPaginateRender) body._tbPaginateRender();
}}
</script>
</body>
</html>"""


async def _moderate_appeal(
    submission_id: str,
    decision: str,
    notes: str,
    admin_user: str,
) -> RedirectResponse:
    db_pool = get_db_pool()
    if not db_pool:
        return RedirectResponse(url="/admin/appeals?err=database", status_code=303)

    clean_id = normalize_submission_id(submission_id)
    if not clean_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", clean_id):
        return RedirectResponse(url="/admin/appeals?err=invalid", status_code=303)

    cleaned_notes = str(notes or "").strip()[:500]
    default_reason = (
        "Phúc khảo đã được Ban Quản Trị phê duyệt."
        if decision == "APPROVED"
        else "Phúc khảo bị từ chối bởi Ban Quản Trị."
    )
    final_reason = cleaned_notes or default_reason

    canonical_id = clean_id
    previous_status = None
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, COALESCE(to_jsonb(s)->>'work_owner', 'legacy'), submission_id, form_id "
                    "FROM survey_submissions s WHERE UPPER(submission_id) = UPPER(%s) FOR UPDATE",
                    (clean_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return RedirectResponse(url="/admin/appeals?err=not_found", status_code=303)
                previous_status = str(row[0] or "").upper()
                work_owner = str(row[1] or "legacy")
                canonical_id = str(row[2] or clean_id)
                form_id = str(row[3] or "")
                if previous_status != "APPEAL_PENDING":
                    return RedirectResponse(url="/admin/appeals?err=not_pending", status_code=303)

                if work_owner == "outbox_v2":
                    refresh_form_registry_from_disk()
                    changed, _state = await transition_submission(
                        conn,
                        canonical_id,
                        decision,
                        final_reason,
                        actor=admin_user,
                        expected_statuses={"APPEAL_PENDING"},
                        schema=FORM_REGISTRY.get(form_id),
                    )
                    if not changed:
                        return RedirectResponse(url="/admin/appeals?err=not_pending", status_code=303)
                else:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = %s, rejection_reason = %s, moderated_at = NOW() "
                        "WHERE UPPER(submission_id) = UPPER(%s) AND status = 'APPEAL_PENDING';",
                        (decision, final_reason, canonical_id),
                    )
                    if int(getattr(cur, "rowcount", 0) or 0) <= 0:
                        return RedirectResponse(url="/admin/appeals?err=not_pending", status_code=303)
            await conn.commit()
    except Exception as exc:
        logger.error("Appeal moderation failed for %s: %s", clean_id, exc)
        return RedirectResponse(url="/admin/appeals?err=unavailable", status_code=303)

    try:
        from routers.survey import _clear_active_appeal_after_verdict
        _clear_active_appeal_after_verdict(canonical_id, decision)
    except Exception:
        pass

    audit_action = "APPEAL_APPROVED" if decision == "APPROVED" else "APPEAL_REJECTED"
    await record_audit_event(
        db_pool,
        "ADMIN_APPEALS",
        audit_action,
        "SURVEY",
        canonical_id,
        "ADMIN",
        admin_user,
        {
            "admin_user": admin_user,
            "submission_id": canonical_id,
            "notes": cleaned_notes or None,
            "previous_status": previous_status,
            "new_status": decision,
        },
    )

    msg_code = "approved" if decision == "APPROVED" else "rejected"
    return RedirectResponse(url=f"/admin/appeals?msg={msg_code}", status_code=303)


@router.get("/admin/appeals", response_class=HTMLResponse)
async def admin_appeals_page(
    request: Request,
    q: str = "",
    msg: str = "",
    err: str = "",
    admin_user: str = Depends(verify_admin_auth),
):
    """Lists submissions awaiting appeal review."""
    refresh_form_registry_from_disk()
    db_pool = get_db_pool()
    rows = await _load_pending_appeals(db_pool, query=q)
    role = getattr(request.state, "admin_role", "viewer")
    can_moderate = role in {"superadmin", "manager", "reviewer"}
    return HTMLResponse(
        content=_render_appeals_page(
            rows,
            query=q,
            msg=msg,
            err=err or ("" if db_pool else "database"),
            can_moderate=can_moderate,
        ),
        status_code=200,
    )


@router.post("/admin/appeals/{submission_id}/approve")
async def approve_appeal(
    submission_id: str,
    notes: str = Form(""),
    admin_user: str = Depends(verify_admin_auth),
):
    """Approves a pending appeal and triggers durable verdict/billing outbox work."""
    return await _moderate_appeal(submission_id, "APPROVED", notes, admin_user)


@router.post("/admin/appeals/{submission_id}/reject")
async def reject_appeal(
    submission_id: str,
    notes: str = Form(""),
    admin_user: str = Depends(verify_admin_auth),
):
    """Rejects a pending appeal and triggers durable verdict outbox work."""
    return await _moderate_appeal(submission_id, "REJECTED", notes, admin_user)
