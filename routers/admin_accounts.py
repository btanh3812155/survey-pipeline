"""Superadmin staff management and authenticated own-password changes."""
import html
import json
import re
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.admin_accounts import (
    ADMIN_ROLES,
    LastActiveSuperadminError,
    create_admin_account,
    get_admin_account,
    list_admin_accounts,
    list_deleted_admin_accounts,
    purge_expired_admin_accounts,
    restore_admin_account,
    set_admin_active,
    soft_delete_admin_account,
    update_admin_password,
    update_admin_role,
)
from core.passwords import hash_password, verify_password
from core.state import get_db_pool, require_admin_request_origin, verify_admin_auth

router = APIRouter(tags=["Admin Accounts"])

MIN_PASSWORD = 12
MAX_PASSWORD = 1024
ACCOUNTS_PATH = "/admin/accounts"
ACCOUNT_PATH = "/admin/account"
ACCOUNT_PASSWORD_PATH = f"{ACCOUNT_PATH}/password"
PASSWORD_PATH = f"{ACCOUNTS_PATH}/password"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

def _valid_password(value: str) -> bool:
    return MIN_PASSWORD <= len(value) <= MAX_PASSWORD and value.isascii() and value.isprintable()

MESSAGES = {
    "password_changed": "Đã đổi mật khẩu. Dùng mật khẩu mới cho lần đăng nhập sau.",
    "account_created": "Đã tạo tài khoản quản trị.",
    "account_deactivated": "Đã tắt tài khoản quản trị.",
    "account_activated": "Đã bật tài khoản quản trị.",
    "account_role_changed": "Đã đổi vai trò tài khoản quản trị.",
    "account_deleted": "Đã chuyển tài khoản vào thùng rác.",
    "account_restored": "Đã khôi phục và bật tài khoản quản trị.",
}
ERRORS = {
    "current_password": "Mật khẩu hiện tại không đúng.",
    "password_policy": f"Mật khẩu mới phải dài từ {MIN_PASSWORD} đến {MAX_PASSWORD} ký tự ASCII in được.",
    "password_mismatch": "Xác nhận mật khẩu không khớp.",
    "same_password": "Mật khẩu mới phải khác mật khẩu hiện tại.",
    "account_exists": "Tên đăng nhập đã tồn tại.",
    "invalid_username": "Tên đăng nhập phải dài 3-32 ký tự, chỉ gồm chữ, số, dấu chấm, gạch dưới, gạch ngang.",
    "invalid_role": "Vai trò không hợp lệ.",
    "last_admin": "Phải giữ ít nhất một tài khoản quản trị đang hoạt động.",
    "self_change": "Không thể tự tắt, xóa hoặc hạ vai trò của mình.",
    "not_found": "Không tìm thấy tài khoản.",
    "db_unavailable": "Cơ sở dữ liệu không khả dụng.",
}


async def _accounts_pool():
    pool = get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail=ERRORS["db_unavailable"])
    await purge_expired_admin_accounts(pool)
    return pool


async def _audit(pool, action: str, actor: str, target: str, **details: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,"
            "actor_type,actor_id,details) VALUES ('admin_accounts',%s,'ADMIN',%s,'ADMIN',%s,%s::jsonb)",
            (action, target[:64], actor[:64], json.dumps({"target": target, **details})),
        )
        await conn.commit()


def _redirect(path: str = ACCOUNTS_PATH, **params: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?{urlencode(params)}", status_code=303)


ROLE_LABELS = {
    "superadmin": "Quản trị",
    "manager": "Quản lý",
    "reviewer": "Kiểm duyệt",
    "viewer": "Chỉ xem",
}


def _role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def _role_options(selected: str = "") -> str:
    return "".join(
        f'<option value="{role}" data-role-label="{role}"'
        f'{" selected" if role == selected else ""}>{html.escape(_role_label(role))}</option>'
        for role in ADMIN_ROLES
    )


def _action_form(
    username: str, operation: str, label: str, *, danger: bool = False,
    fields: str = "", label_key: str = "",
) -> str:
    action = html.escape(f"{ACCOUNTS_PATH}/{quote(username, safe='')}/{operation}")
    button_class = ' class="danger"' if danger else ""
    label_attr = f' data-i18n="{label_key}"' if label_key else ""
    return (
        f'<form method="post" action="{action}">{fields}'
        f'<button type="submit"{button_class}{label_attr}>{label}</button></form>'
    )


_ACCOUNT_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _render(
    title: str, admin_user: str, content: str, msg: str = "", err: str = "",
    title_key: str = "",
) -> str:
    notice = ""
    if msg in MESSAGES:
        notice = f'<div class="notice ok" data-i18n="msg_{msg}">{html.escape(MESSAGES[msg])}</div>'
    if err in ERRORS:
        notice = f'<div class="notice bad" data-i18n="err_{err}">{html.escape(ERRORS[err])}</div>'
    title = html.escape(title)
    title_attr = f' data-i18n="{title_key}"' if title_key else ""

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
:root {{ --bg:#090d16; --surface:#131b2e; --border:#23314f; --text:#f8fafc; --muted:#94a3b8; --accent:#2563eb; --danger:#ef4444; --ok:#10b981; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px; }}
h1 {{ font-size:1.3rem; margin:0 0 4px; }}
p.sub {{ color:var(--muted); margin:0 0 20px; font-size:.85rem; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; }}
.card h2 {{ font-size:.95rem; margin:0 0 12px; color:#93c5fd; }}
a {{ color:#93c5fd; }}
label {{ display:block; font-size:.8rem; color:var(--muted); margin-bottom:10px; }}
input,select {{ margin-top:4px; padding:9px 10px; border-radius:8px; border:1px solid var(--border); background:#0b1220; color:var(--text); }}
input {{ width:100%; }}
button {{ padding:8px 12px; border-radius:8px; border:1px solid var(--accent); background:var(--accent); color:#fff; font-weight:600; cursor:pointer; }}
button.danger {{ background:transparent; border-color:var(--danger); color:#f87171; }}
table {{ width:100%; border-collapse:collapse; background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; }}
th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:.84rem; }}
th {{ color:var(--muted); font-weight:600; }}
tr:last-child td {{ border-bottom:none; }}
.badge {{ padding:2px 8px; border-radius:999px; font-size:.72rem; }}
.badge.on {{ background:rgba(16,185,129,.15); color:#6ee7b7; }}
.badge.off {{ background:rgba(239,68,68,.15); color:#fca5a5; }}
.actions {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; }}
.inline-form {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; align-items:end; }}
.inline-form button {{ align-self:end; }}
.manage summary {{ cursor:pointer; padding:8px 12px; border:1px solid var(--border); border-radius:8px; color:#93c5fd; font-weight:700; list-style:none; }}
.manage summary::-webkit-details-marker {{ display:none; }}
.manage[open] summary {{ border-color:var(--accent); }}
.manage-body {{ margin-top:10px; padding:12px; border:1px solid var(--border); border-radius:10px; background:#0b1220; min-width:260px; }}
.manage-body form {{ margin-bottom:10px; }}
.muted {{ color:var(--muted); font-size:.78rem; }}
.meta-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
.meta-item {{ background:#0b1220; border:1px solid var(--border); border-radius:10px; padding:10px 12px; min-width:0; }}
.meta-key {{ color:var(--muted); font-size:.74rem; font-weight:600; margin-bottom:4px; }}
.meta-value {{ font-weight:700; overflow-wrap:anywhere; }}
.recovery-card {{ border-color:rgba(245,158,11,.45); }}
.security-note {{ color:var(--muted); font-size:.8rem; line-height:1.5; margin:0 0 12px; }}
.recovery-actions {{ margin-top:12px; }}
.button-link {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:8px; background:var(--accent); color:#fff; font-weight:700; text-decoration:none; }}
.notice {{ padding:10px 12px; border-radius:8px; margin-bottom:16px; font-size:.85rem; }}
.notice.ok {{ background:rgba(16,185,129,.15); color:#a7f3d0; }}
.notice.bad {{ background:rgba(239,68,68,.15); color:#fecaca; }}
</style>
</head>
<body>
<main>
<h1{title_attr}>{title}</h1>
<p class="sub"><span data-i18n="logged_in">Đang đăng nhập:</span> <code>{html.escape(admin_user)}</code></p>
{notice}
{content}
</main>
</body>
</html>"""


def _date(value) -> str:
    return html.escape(value.strftime("%d/%m/%Y %H:%M")) if value else "-"


def _password_form(action: str) -> str:
    return f"""
    <form method="post" action="{action}">
      <label><span data-i18n="current_password">Mật khẩu hiện tại</span><input type="password" name="current_password" required autocomplete="current-password"></label>
      <label><span data-i18n="new_password_hint">Mật khẩu mới (12–1024 ký tự ASCII in được)</span><input type="password" name="new_password" minlength="{MIN_PASSWORD}" maxlength="{MAX_PASSWORD}" required autocomplete="new-password"></label>
      <label><span data-i18n="confirm_new_password">Nhập lại mật khẩu mới</span><input type="password" name="confirm_password" minlength="{MIN_PASSWORD}" maxlength="{MAX_PASSWORD}" required autocomplete="new-password"></label>
      <button type="submit" data-i18n="change_password">Đổi mật khẩu</button>
    </form>"""


def _manage_panel(username: str, admin_user: str, role: str, active: bool) -> str:
    if username == admin_user:
        body = _password_form(ACCOUNT_PASSWORD_PATH)
        body += f'<p><a href="{ACCOUNT_PATH}" data-i18n="open_my_account">Mở trang tài khoản của tôi</a></p>'
    else:
        body = _action_form(
            username, "role", "Cập nhật vai trò", label_key="update_role_short",
            fields=f'<label><span data-i18n="new_role">Vai trò mới</span><select name="role">{_role_options(role)}</select></label>',
        )
        body += (
            _action_form(username, "deactivate", "Tắt tài khoản", danger=True, label_key="deactivate_account") if active
            else _action_form(username, "activate", "Bật tài khoản", label_key="activate_account")
        )
        body += _action_form(username, "delete", "Xóa tài khoản", danger=True, label_key="delete_account")
    return f'<details class="manage"><summary data-i18n="manage">Quản lý</summary><div class="manage-body">{body}</div></details>'


def _render_accounts(admin_user: str, accounts, deleted, msg: str, err: str) -> str:
    role_accounts = "".join(
        f'<option value="{html.escape(str(account["username"]))}" '
        f'data-account-option data-account-username="{html.escape(str(account["username"]))}" '
        f'data-account-role="{html.escape(str(account["role"]))}">'
        f'{html.escape(str(account["username"]))} ({html.escape(_role_label(str(account["role"])))})</option>'
        for account in accounts
    )
    rows = []
    for account in accounts:
        username = account["username"]
        role = account["role"]
        active = account["is_active"]
        actions = _manage_panel(username, admin_user, role, active)
        rows.append(
            f'<tr><td><code>{html.escape(username)}</code></td>'
            f'<td><span data-role-label="{html.escape(str(role))}">{html.escape(_role_label(str(role)))}</span></td>'
            f'<td><span class="badge {"on" if active else "off"}" data-i18n="{"status_on" if active else "status_off"}">{"Hoạt động" if active else "Đã tắt"}</span></td>'
            f'<td>{_date(account["created_at"])}</td><td><div class="actions">{actions}</div></td></tr>'
        )
    bin_rows = []
    for account in deleted:
        username = account["username"]
        bin_rows.append(
            f'<tr><td><code>{html.escape(username)}</code></td>'
            f'<td><span data-role-label="{html.escape(str(account["role"]))}">{html.escape(_role_label(str(account["role"])))}</span></td>'
            f'<td>{_date(account["deleted_at"])}</td><td>{html.escape(account["deleted_by"] or "-")}</td>'
            f'<td>{_action_form(username, "restore", "Khôi phục", label_key="restore")}</td></tr>'
        )
    empty_row = '<tr><td colspan="5" class="muted" data-i18n="empty_accounts">Không có tài khoản.</td></tr>'
    content = f"""
<section class="card">
  <h2 data-i18n="account_create_heading">Tạo tài khoản quản trị</h2>
  <form method="post" action="{ACCOUNTS_PATH}">
    <label><span data-i18n="username">Tên đăng nhập</span><input type="text" name="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9._\\-]+" required autocomplete="off"></label>
    <label><span data-i18n="role">Vai trò</span><select name="role" required><option value="" disabled selected data-i18n="choose_role">Chọn vai trò</option>{_role_options()}</select></label>
    <label><span data-i18n="password_hint">Mật khẩu (12–1024 ký tự ASCII in được)</span><input type="password" name="password" minlength="{MIN_PASSWORD}" maxlength="{MAX_PASSWORD}" required autocomplete="new-password"></label>
    <label><span data-i18n="confirm_password">Nhập lại mật khẩu</span><input type="password" name="confirm_password" minlength="{MIN_PASSWORD}" maxlength="{MAX_PASSWORD}" required autocomplete="new-password"></label>
    <button type="submit" data-i18n="create_account">Tạo tài khoản</button>
  </form>
</section>
<section class="card">
  <h2 data-i18n="role_section_heading">Phân quyền tài khoản</h2>
  <p class="muted" data-i18n="role_section_hint">Chọn tài khoản và vai trò mới. Không thể tự hạ vai trò hoặc xóa quản trị cuối cùng.</p>
  <form method="post" action="{ACCOUNTS_PATH}/role" class="inline-form">
    <label><span data-i18n="account">Tài khoản</span><select name="username" required>{role_accounts}</select></label>
    <label><span data-i18n="new_role">Vai trò mới</span><select name="role" required><option value="" disabled selected data-i18n="choose_role">Chọn vai trò</option>{_role_options()}</select></label>
    <button type="submit" data-i18n="update_role">Cập nhật vai trò</button>
  </form>
</section>
<h2 data-i18n="accounts_table_heading">Tài khoản quản trị</h2>
<table>
  <thead><tr><th data-i18n="th_username">Tên đăng nhập</th><th data-i18n="th_role">Vai trò</th><th data-i18n="th_status">Trạng thái</th><th data-i18n="th_created">Tạo lúc</th><th></th></tr></thead>
  <tbody>{''.join(rows) or empty_row}</tbody>
</table>
<h2 data-i18n="trash_heading">Thùng rác</h2>
<p class="sub" data-i18n="trash_hint">Có thể khôi phục tài khoản trong vòng 30 ngày sau khi xóa.</p>
<table>
  <thead><tr><th data-i18n="th_username">Tên đăng nhập</th><th data-i18n="th_role">Vai trò</th><th data-i18n="th_deleted_at">Xóa lúc</th><th data-i18n="th_deleted_by">Người xóa</th><th></th></tr></thead>
  <tbody>{''.join(bin_rows) or empty_row}</tbody>
</table>"""
    return _render("Quản lý tài khoản quản trị", admin_user, content, msg, err, title_key="accounts_title")


def _render_own_account(admin_user: str, account, msg: str = "", err: str = "") -> str:
    role = str(account.get("role") or "")
    active = bool(account.get("is_active"))
    status_key = "status_on" if active else "status_off"
    status_text = "Hoạt động" if active else "Đã tắt"
    recovery_block = ""
    if role == "superadmin":
        recovery_email = str(account.get("recovery_email") or "").strip()
        recovery_value = (
            f"<code>{html.escape(recovery_email)}</code>"
            if recovery_email
            else '<span class="muted" data-i18n="recovery_email_unset">Chưa cấu hình</span>'
        )
        recovery_block = f"""
<section class="card recovery-card">
  <h2 data-i18n="recovery_email_heading">Email khôi phục</h2>
  <p class="security-note" data-i18n="recovery_email_security">Chỉ quản trị thấy mục này. Thay đổi email cần xác nhận từ hộp thư hiện tại và hộp thư mới.</p>
  <div class="meta-grid">
    <div class="meta-item">
      <div class="meta-key" data-i18n="recovery_email_label">Email khôi phục hiện tại</div>
      <div class="meta-value">{recovery_value}</div>
    </div>
  </div>
  <div class="recovery-actions">
    <a class="button-link" href="/admin/recovery/email" data-i18n="change_recovery_email">Đổi email khôi phục</a>
  </div>
</section>"""
    content = f"""
<section class="card">
  <h2 data-i18n="account_self_heading">Tài khoản của tôi</h2>
  <div class="meta-grid">
    <div class="meta-item">
      <div class="meta-key" data-i18n="username">Tên đăng nhập</div>
      <div class="meta-value"><code>{html.escape(admin_user)}</code></div>
    </div>
    <div class="meta-item">
      <div class="meta-key" data-i18n="role">Vai trò</div>
      <div class="meta-value"><span data-role-label="{html.escape(role)}">{html.escape(_role_label(role))}</span></div>
    </div>
    <div class="meta-item">
      <div class="meta-key" data-i18n="account_status">Trạng thái</div>
      <div class="meta-value"><span class="badge {'on' if active else 'off'}" data-i18n="{status_key}">{status_text}</span></div>
    </div>
    <div class="meta-item">
      <div class="meta-key" data-i18n="account_updated">Cập nhật</div>
      <div class="meta-value">{_date(account.get("updated_at"))}</div>
    </div>
  </div>
</section>
{recovery_block}
<section class="card">
  <h2 data-i18n="change_password_heading">Đổi mật khẩu</h2>
  {_password_form(ACCOUNT_PASSWORD_PATH)}
</section>"""
    return _render("Tài khoản của tôi", admin_user, content, msg, err, title_key="account_title")


@router.get(ACCOUNTS_PATH, response_class=HTMLResponse)
async def admin_accounts_page(msg: str = "", err: str = "", admin_user: str = Depends(verify_admin_auth)):
    pool = await _accounts_pool()
    accounts = await list_admin_accounts(pool)
    deleted = await list_deleted_admin_accounts(pool)
    return HTMLResponse(_render_accounts(admin_user, accounts, deleted, msg, err), headers=_ACCOUNT_HEADERS)


@router.get(ACCOUNT_PATH, response_class=HTMLResponse)
async def admin_own_account_page(
    msg: str = "", err: str = "", admin_user: str = Depends(verify_admin_auth)
):
    pool = await _accounts_pool()
    account = await get_admin_account(pool, admin_user)
    if not account:
        raise HTTPException(status_code=401, detail="Tài khoản không còn hoạt động.")
    return HTMLResponse(_render_own_account(admin_user, account, msg=msg, err=err), headers=_ACCOUNT_HEADERS)


@router.get(PASSWORD_PATH)
async def legacy_password_page(admin_user: str = Depends(verify_admin_auth)):
    return RedirectResponse(url=ACCOUNT_PATH, status_code=303)


@router.post(ACCOUNT_PASSWORD_PATH, response_class=HTMLResponse)
@router.post(PASSWORD_PATH, response_class=HTMLResponse)
async def change_admin_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    admin_user: str = Depends(verify_admin_auth),
):
    require_admin_request_origin(request)
    pool = await _accounts_pool()
    if not _valid_password(new_password):
        return _redirect(ACCOUNT_PATH, err="password_policy")
    if new_password != confirm_password:
        return _redirect(ACCOUNT_PATH, err="password_mismatch")
    current_hash = request.state.admin_password_hash
    if not verify_password(current_password, current_hash):
        return _redirect(ACCOUNT_PATH, err="current_password")
    if current_password == new_password:
        return _redirect(ACCOUNT_PATH, err="same_password")
    if not await update_admin_password(pool, admin_user, hash_password(new_password), expected_hash=current_hash):
        return _redirect(ACCOUNT_PATH, err="current_password")
    await _audit(pool, "ADMIN_PASSWORD_CHANGED", admin_user, admin_user)
    # Basic auth may still send the old password on this response.
    account = await get_admin_account(pool, admin_user)
    return HTMLResponse(_render_own_account(admin_user, account or {}, msg="password_changed"), headers=_ACCOUNT_HEADERS)


@router.post(ACCOUNTS_PATH)
async def create_account(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
    admin_user: str = Depends(verify_admin_auth),
):
    require_admin_request_origin(request)
    pool = await _accounts_pool()
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        return _redirect(err="invalid_username")
    if role not in ADMIN_ROLES:
        return _redirect(err="invalid_role")
    if not _valid_password(password):
        return _redirect(err="password_policy")
    if password != confirm_password:
        return _redirect(err="password_mismatch")
    created = await create_admin_account(pool, username, hash_password(password), role)
    if not created:
        return _redirect(err="account_exists")
    await _audit(pool, "ADMIN_ACCOUNT_CREATED", admin_user, username, role=role)
    return _redirect(msg="account_created")


@router.post(ACCOUNTS_PATH + "/role")
async def change_account_role(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    admin_user: str = Depends(verify_admin_auth),
):
    require_admin_request_origin(request)
    pool = await _accounts_pool()
    username = username.strip()
    if role not in ADMIN_ROLES:
        return _redirect(err="invalid_role")
    if username == admin_user and role != "superadmin":
        return _redirect(err="self_change")
    try:
        changed = await update_admin_role(pool, username, role)
    except LastActiveSuperadminError:
        return _redirect(err="last_admin")
    if not changed:
        return _redirect(err="not_found")
    await _audit(pool, "ADMIN_ACCOUNT_ROLE_CHANGED", admin_user, username, role=role)
    return _redirect(msg="account_role_changed")


@router.post(ACCOUNTS_PATH + "/{username}/{operation}")
async def manage_account(
    username: str,
    operation: Literal["role", "activate", "deactivate", "delete", "restore"],
    request: Request,
    role: str = Form(""),
    admin_user: str = Depends(verify_admin_auth),
):
    require_admin_request_origin(request)
    pool = await _accounts_pool()
    if operation == "role" and role not in ADMIN_ROLES:
        return _redirect(err="invalid_role")
    if username == admin_user and (
        operation in {"deactivate", "delete"} or (operation == "role" and role != "superadmin")
    ):
        return _redirect(err="self_change")
    details = {}
    try:
        if operation == "role":
            changed = await update_admin_role(pool, username, role)
            event = "ROLE_CHANGED"
            details["role"] = role
        elif operation == "delete":
            changed = await soft_delete_admin_account(pool, username, admin_user)
            event = "DELETED"
        elif operation == "restore":
            changed = await restore_admin_account(pool, username)
            event = "RESTORED"
        else:
            active = operation == "activate"
            changed = await set_admin_active(pool, username, active)
            event = "ACTIVATED" if active else "DEACTIVATED"
    except LastActiveSuperadminError:
        return _redirect(err="last_admin")
    if not changed:
        return _redirect(err="not_found")
    await _audit(pool, f"ADMIN_ACCOUNT_{event}", admin_user, username, **details)
    return _redirect(msg=f"account_{event.lower()}")
