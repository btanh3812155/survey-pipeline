# -*- coding: utf-8 -*-
"""
Superadmin Payouts & Revocation Router.

Lists collaborator billing ledger entries joined with survey submissions,
allows superadmins to update payout settlement status, and supports
transactional revocation of APPROVED/PAID records with full audit logging.
"""

import html
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.durable_work import transition_submission
from core.state import FORM_REGISTRY, get_db_pool, refresh_form_registry_from_disk, verify_admin_auth
from core.submission_ids import normalize_submission_id
from services.audit_and_privacy import record_audit_event

logger = logging.getLogger("routers.admin_payouts")
router = APIRouter(tags=["Admin Payouts"])

VALID_PAYOUT_STATUSES = ("UNPAID", "PENDING_PAYOUT", "PAID", "CANCELLED")
DEFAULT_REVOKE_REASON = "Thu hồi bởi quản trị viên"

MESSAGES = {
    "status_updated": ("ok", "Đã cập nhật trạng thái chi trả CTV."),
    "revoked": ("ok", "Đã thu hồi phiếu khảo sát và hủy chi trả thành công."),
}

ERRORS = {
    "not_found": "Không tìm thấy bản ghi chi trả hoặc phiếu khảo sát.",
    "invalid": "Mã phiếu không hợp lệ.",
    "invalid_status": "Trạng thái chi trả không hợp lệ.",
    "database": "Cơ sở dữ liệu chưa sẵn sàng.",
    "unavailable": "Không thể xử lý yêu cầu lúc này. Vui lòng thử lại.",
}


def _fmt_ts(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y %H:%M")
    return str(value) if value else "-"


def _fmt_vnd(amount: Any) -> str:
    try:
        return f"{int(amount or 0):,} VND"
    except (TypeError, ValueError):
        return "0 VND"


def _status_badge_class(value: str) -> str:
    upper = str(value or "").upper()
    if upper in {"PAID", "APPROVED"}:
        return "approved"
    if upper in {"CANCELLED", "REJECTED"}:
        return "rejected"
    return "review"


def _page_flash(msg: str = "", err: str = "") -> str:
    if msg and msg in MESSAGES:
        kind, text = MESSAGES[msg]
        return f'<div class="notice {kind}" role="status">{html.escape(text)}</div>'
    if err:
        text = ERRORS.get(err, err)
        return f'<div class="notice bad" role="alert">{html.escape(text)}</div>'
    return ""


async def _load_payout_rows(
    db_pool,
    *,
    query: str = "",
    payout_status: str = "",
) -> List[Dict[str, Any]]:
    if not db_pool:
        return []
    q = str(query or "").strip()
    status_filter = str(payout_status or "").strip().upper()
    if status_filter not in VALID_PAYOUT_STATUSES:
        status_filter = ""

    clauses: List[str] = []
    params: List[Any] = []
    if status_filter:
        clauses.append("b.payout_status = %s")
        params.append(status_filter)
    if q:
        like = f"%{q}%"
        clauses.append(
            "("
            "CAST(b.transaction_id AS TEXT) ILIKE %s "
            "OR b.submission_id ILIKE %s "
            "OR COALESCE(s.form_id, '') ILIKE %s "
            "OR COALESCE(s.ctv_code, '') ILIKE %s "
            "OR COALESCE(CAST(b.collaborator_id AS TEXT), '') ILIKE %s "
            "OR COALESCE(b.payout_status, '') ILIKE %s "
            "OR COALESCE(s.status, '') ILIKE %s"
            ")"
        )
        params.extend([like] * 7)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql_query = f"""
        SELECT b.transaction_id,
               b.submission_id,
               s.form_id,
               s.ctv_code,
               b.collaborator_id,
               b.payout_amount_vnd,
               b.payout_status,
               s.status AS submission_status,
               b.created_at
        FROM collaborator_billing_ledger b
        LEFT JOIN survey_submissions s ON s.submission_id = b.submission_id
        {where_sql}
        ORDER BY b.created_at DESC, b.submission_id DESC
        LIMIT 500;
    """
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql_query, tuple(params))
                rows = await cur.fetchall()
    except Exception as exc:
        logger.warning("Failed to query collaborator_billing_ledger: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "transaction_id": str(row[0] or ""),
                "submission_id": str(row[1] or ""),
                "form_id": str(row[2] or ""),
                "ctv_code": str(row[3] or ""),
                "collaborator_id": row[4],
                "payout_amount_vnd": int(row[5] or 0),
                "payout_status": str(row[6] or "UNPAID"),
                "submission_status": str(row[7] or ""),
                "created_at": row[8],
            }
        )
    return results


def _render_payouts_page(
    rows: List[Dict[str, Any]],
    *,
    query: str = "",
    payout_status: str = "",
    msg: str = "",
    err: str = "",
) -> str:
    counts = {st: 0 for st in VALID_PAYOUT_STATUSES}
    for row in rows:
        st = row["payout_status"]
        if st in counts:
            counts[st] += 1

    body_rows: List[str] = []
    for row in rows:
        tx_raw = row["transaction_id"]
        sub_raw = row["submission_id"]
        sub_esc = html.escape(sub_raw)
        sub_path = html.escape(quote(sub_raw, safe=""), quote=True)
        form_id_raw = row["form_id"]
        schema = FORM_REGISTRY.get(form_id_raw)
        form_title = schema.title if schema else ""
        form_cell = f"<code>{html.escape(form_id_raw or '-')}</code>"
        if form_title:
            form_cell += f'<div class="muted">{html.escape(form_title)}</div>'

        ctv_raw = row["ctv_code"]
        collab_raw = "" if row["collaborator_id"] is None else str(row["collaborator_id"])
        ctv_parts = []
        if ctv_raw:
            ctv_parts.append(f"<code>{html.escape(ctv_raw)}</code>")
        if collab_raw:
            ctv_parts.append(f'<div class="muted">TG: <code>{html.escape(collab_raw)}</code></div>')
        ctv_cell = "".join(ctv_parts) or '<span class="muted">-</span>'

        payout_st = row["payout_status"]
        sub_st = row["submission_status"] or "-"
        amount_text = html.escape(_fmt_vnd(row["payout_amount_vnd"]))
        created_text = html.escape(_fmt_ts(row["created_at"]))

        status_options = "".join(
            f'<option value="{st}"{" selected" if st == payout_st else ""}>{st}</option>'
            for st in VALID_PAYOUT_STATUSES
        )
        details_href = f"/admin/submissions?search={quote(sub_raw, safe='')}"
        search_blob = html.escape(
            f"{tx_raw} {sub_raw} {form_id_raw} {form_title} {ctv_raw} {collab_raw} {payout_st} {sub_st}".lower(),
            quote=True,
        )

        body_rows.append(
            f"""
            <tr data-payout-row data-search="{search_blob}" data-payout-status="{html.escape(payout_st, quote=True)}">
              <td>
                <code>{sub_esc}</code>
                <div class="muted" style="font-size:0.75rem;margin-top:4px;">TX: <code>{html.escape(tx_raw[:12])}</code></div>
                <div class="muted" style="margin-top:4px;"><a href="{details_href}">Xem hồ sơ</a></div>
              </td>
              <td>{form_cell}</td>
              <td>{ctv_cell}</td>
              <td><strong>{amount_text}</strong></td>
              <td>
                <div><span class="status {_status_badge_class(payout_st)}">{html.escape(payout_st)}</span></div>
                <div class="muted" style="margin-top:6px;">Phiếu: <span class="status {_status_badge_class(sub_st)}">{html.escape(sub_st)}</span></div>
              </td>
              <td>{created_text}</td>
              <td style="min-width:290px;">
                <form method="post" action="/admin/payouts/{sub_path}/status" class="payout-action-form">
                  <div class="payout-inline">
                    <select name="payout_status" data-native-select aria-label="Trạng thái chi trả cho phiếu {sub_esc}">
                      {status_options}
                    </select>
                    <button type="submit" class="secondary">Cập nhật trạng thái</button>
                  </div>
                  <input type="text" name="notes" maxlength="500" placeholder="Ghi chú quyết toán (tùy chọn)..." aria-label="Ghi chú quyết toán cho phiếu {sub_esc}">
                </form>
                <form method="post" action="/admin/payouts/{sub_path}/revoke" class="payout-action-form payout-revoke-form">
                  <input type="text" name="reason" maxlength="500" placeholder="Lý do thu hồi..." aria-label="Lý do thu hồi cho phiếu {sub_esc}">
                  <button type="submit" class="danger">Thu hồi phiếu &amp; hủy chi trả</button>
                </form>
              </td>
            </tr>
            """
        )

    table_body = "".join(body_rows) or (
        '<tr><td colspan="7" class="empty" style="text-align:center;padding:28px;">'
        "Chưa có bản ghi chi trả nào trong sổ cái."
        "</td></tr>"
    )
    query_esc = html.escape(query, quote=True)
    status_filter_options = '<option value="">Tất cả trạng thái chi trả</option>' + "".join(
        f'<option value="{st}"{" selected" if st == payout_status else ""}>{st}</option>'
        for st in VALID_PAYOUT_STATUSES
    )

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quản Lý Chi Trả CTV - Trust Bridge</title>
<style>
  .payout-toolbar {{
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }}
  .payout-toolbar input[type="search"] {{
    flex: 1 1 280px;
    margin-top: 0;
  }}
  .payout-toolbar select {{
    width: auto;
    min-width: 200px;
    margin-top: 0;
  }}
  .payout-action-form {{
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  .payout-revoke-form {{
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }}
  .payout-inline {{
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .payout-action-form select,
  .payout-action-form input[type="text"] {{
    margin-top: 0;
    font-size: 0.82rem !important;
    min-height: 34px;
  }}
  .payout-inline select {{
    flex: 1 1 140px;
  }}
</style>
</head>
<body>
<main>
  <header class="admin-hero">
    <div>
      <h1>Quản Lý Chi Trả CTV</h1>
      <p class="sub">Đối soát sổ cái thù lao CTV, cập nhật trạng thái chi trả và thu hồi phiếu đã duyệt hoặc đã thanh toán.</p>
    </div>
    <div class="hero-actions">
      <a class="button secondary" href="/admin/submissions">Dữ liệu nộp</a>
      <a class="button secondary" href="/admin/payouts">Làm Mới</a>
    </div>
  </header>

  {_page_flash(msg, err)}

  <section class="stats-grid" aria-label="Tổng quan chi trả">
    <div class="stat-card total">
      <div class="stat-title">Tổng bản ghi</div>
      <div class="stat-value">{len(rows)}</div>
    </div>
    <div class="stat-card review">
      <div class="stat-title">UNPAID</div>
      <div class="stat-value">{counts["UNPAID"]}</div>
    </div>
    <div class="stat-card ingress">
      <div class="stat-title">PENDING_PAYOUT</div>
      <div class="stat-value">{counts["PENDING_PAYOUT"]}</div>
    </div>
    <div class="stat-card approved">
      <div class="stat-title">PAID</div>
      <div class="stat-value">{counts["PAID"]}</div>
    </div>
    <div class="stat-card rejected">
      <div class="stat-title">CANCELLED</div>
      <div class="stat-value">{counts["CANCELLED"]}</div>
    </div>
  </section>

  <form class="payout-toolbar" method="get" action="/admin/payouts">
    <input type="search" id="payoutSearch" name="q" value="{query_esc}" placeholder="Tìm theo mã giao dịch, mã phiếu, form, mã CTV hoặc Telegram ID..." oninput="filterPayoutRows()">
    <select id="payoutStatusFilter" name="status" data-native-select aria-label="Lọc trạng thái chi trả" onchange="filterPayoutRows()">
      {status_filter_options}
    </select>
    <button type="submit" class="secondary">Tìm kiếm</button>
  </form>

  <section class="card table-card">
    <table>
      <thead>
        <tr>
          <th>Mã phiếu</th>
          <th>Biểu mẫu</th>
          <th>CTV</th>
          <th>Số tiền</th>
          <th>Trạng thái</th>
          <th>Thời gian</th>
          <th>Thao tác</th>
        </tr>
      </thead>
      <tbody id="payoutsTableBody" data-tb-paginate="tr[data-payout-row]" data-page-size="15">
        {table_body}
      </tbody>
    </table>
  </section>
</main>
<script>
function filterPayoutRows() {{
  const q = (document.getElementById('payoutSearch').value || '').trim().toLowerCase();
  const st = (document.getElementById('payoutStatusFilter').value || '').trim().toUpperCase();
  const body = document.getElementById('payoutsTableBody');
  document.querySelectorAll('[data-payout-row]').forEach((row) => {{
    const matchText = !q || (row.dataset.search || '').includes(q);
    const matchStatus = !st || (row.dataset.payoutStatus || '').toUpperCase() === st;
    const ok = matchText && matchStatus;
    row.dataset.filterHidden = ok ? '0' : '1';
    if (!body?._tbPaginateRender) row.hidden = !ok;
  }});
  if (body?._tbPaginateRender) body._tbPaginateRender();
}}
</script>
</body>
</html>"""


@router.get("/admin/payouts", response_class=HTMLResponse)
async def admin_payouts_page(
    request: Request,
    q: str = "",
    status: str = "",
    msg: str = "",
    err: str = "",
    admin_user: str = Depends(verify_admin_auth),
):
    """Superadmin view of collaborator billing ledger entries and revocation controls."""
    refresh_form_registry_from_disk()
    db_pool = get_db_pool()
    clean_status = str(status or "").strip().upper()
    if clean_status not in VALID_PAYOUT_STATUSES:
        clean_status = ""
    rows = await _load_payout_rows(db_pool, query=q, payout_status=clean_status)
    return HTMLResponse(
        content=_render_payouts_page(
            rows,
            query=q,
            payout_status=clean_status,
            msg=msg,
            err=err or ("" if db_pool else "database"),
        ),
        status_code=200,
    )


@router.post("/admin/payouts/{submission_id}/status")
async def update_payout_status(
    submission_id: str,
    payout_status: str = Form(...),
    notes: str = Form(""),
    admin_user: str = Depends(verify_admin_auth),
):
    """Superadmin updates payout_status on collaborator_billing_ledger with audit logging."""
    db_pool = get_db_pool()
    if not db_pool:
        return RedirectResponse(url="/admin/payouts?err=database", status_code=303)

    clean_id = normalize_submission_id(submission_id)
    if not clean_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", clean_id):
        return RedirectResponse(url="/admin/payouts?err=invalid", status_code=303)

    new_status = str(payout_status or "").strip().upper()
    if new_status not in VALID_PAYOUT_STATUSES:
        return RedirectResponse(url="/admin/payouts?err=invalid_status", status_code=303)

    cleaned_notes = str(notes or "").strip()[:500]
    canonical_id = clean_id
    previous_payout_status: Optional[str] = None

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT submission_id, payout_status "
                    "FROM collaborator_billing_ledger "
                    "WHERE UPPER(submission_id) = UPPER(%s) FOR UPDATE",
                    (clean_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return RedirectResponse(url="/admin/payouts?err=not_found", status_code=303)
                canonical_id = str(row[0] or clean_id)
                previous_payout_status = str(row[1] or "")
                await cur.execute(
                    "UPDATE collaborator_billing_ledger SET payout_status = %s "
                    "WHERE UPPER(submission_id) = UPPER(%s)",
                    (new_status, canonical_id),
                )
            await conn.commit()
    except Exception as exc:
        logger.error("Payout status update failed for %s: %s", clean_id, exc)
        return RedirectResponse(url="/admin/payouts?err=unavailable", status_code=303)

    await record_audit_event(
        db_pool,
        "ADMIN_PAYOUTS",
        "PAYOUT_STATUS_CHANGED",
        "PAYOUT",
        canonical_id,
        "ADMIN",
        admin_user,
        {
            "admin_user": admin_user,
            "submission_id": canonical_id,
            "previous_payout_status": previous_payout_status,
            "new_payout_status": new_status,
            "notes": cleaned_notes or None,
        },
    )
    return RedirectResponse(url="/admin/payouts?msg=status_updated", status_code=303)


@router.post("/admin/payouts/{submission_id}/revoke")
async def revoke_payout_and_submission(
    submission_id: str,
    reason: str = Form(""),
    admin_user: str = Depends(verify_admin_auth),
):
    """Superadmin revokes an APPROVED submission and cancels its billing ledger row."""
    db_pool = get_db_pool()
    if not db_pool:
        return RedirectResponse(url="/admin/payouts?err=database", status_code=303)

    clean_id = normalize_submission_id(submission_id)
    if not clean_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", clean_id):
        return RedirectResponse(url="/admin/payouts?err=invalid", status_code=303)

    final_reason = str(reason or "").strip()[:500] or DEFAULT_REVOKE_REASON
    canonical_id = clean_id
    previous_submission_status: Optional[str] = None
    previous_payout_status: Optional[str] = None

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, COALESCE(to_jsonb(s)->>'work_owner', 'legacy'), submission_id, form_id "
                    "FROM survey_submissions s WHERE UPPER(submission_id) = UPPER(%s) FOR UPDATE",
                    (clean_id,),
                )
                sub_row = await cur.fetchone()
                await cur.execute(
                    "SELECT submission_id, payout_status "
                    "FROM collaborator_billing_ledger "
                    "WHERE UPPER(submission_id) = UPPER(%s) FOR UPDATE",
                    (clean_id,),
                )
                ledger_row = await cur.fetchone()

                if not sub_row and not ledger_row:
                    return RedirectResponse(url="/admin/payouts?err=not_found", status_code=303)

                if sub_row:
                    previous_submission_status = str(sub_row[0] or "").upper()
                    work_owner = str(sub_row[1] or "legacy")
                    canonical_id = str(sub_row[2] or clean_id)
                    form_id = str(sub_row[3] or "")

                    if work_owner == "outbox_v2" and previous_submission_status != "REJECTED":
                        refresh_form_registry_from_disk()
                        await transition_submission(
                            conn,
                            canonical_id,
                            "REJECTED",
                            final_reason,
                            actor=admin_user,
                            schema=FORM_REGISTRY.get(form_id),
                        )
                    else:
                        await cur.execute(
                            "UPDATE survey_submissions "
                            "SET status = 'REJECTED', rejection_reason = %s, moderated_at = NOW() "
                            "WHERE UPPER(submission_id) = UPPER(%s)",
                            (final_reason, canonical_id),
                        )

                if ledger_row:
                    if not sub_row:
                        canonical_id = str(ledger_row[0] or clean_id)
                    previous_payout_status = str(ledger_row[1] or "")
                    await cur.execute(
                        "UPDATE collaborator_billing_ledger SET payout_status = 'CANCELLED' "
                        "WHERE UPPER(submission_id) = UPPER(%s)",
                        (canonical_id,),
                    )
            await conn.commit()
    except Exception as exc:
        logger.error("Payout revocation failed for %s: %s", clean_id, exc)
        return RedirectResponse(url="/admin/payouts?err=unavailable", status_code=303)

    try:
        from routers.survey import _clear_active_appeal_after_verdict
        _clear_active_appeal_after_verdict(canonical_id, "REJECTED")
    except Exception:
        pass

    await record_audit_event(
        db_pool,
        "ADMIN_PAYOUTS",
        "PAYOUT_REVOKED",
        "PAYOUT",
        canonical_id,
        "ADMIN",
        admin_user,
        {
            "admin_user": admin_user,
            "submission_id": canonical_id,
            "previous_submission_status": previous_submission_status,
            "previous_payout_status": previous_payout_status,
            "new_submission_status": "REJECTED",
            "new_payout_status": "CANCELLED",
            "reason": final_reason,
        },
    )
    return RedirectResponse(url="/admin/payouts?msg=revoked", status_code=303)
