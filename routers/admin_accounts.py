"""Admin account management: authenticated password change and account status."""
import html
import json
import re
import secrets
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from core.admin_accounts import (
    count_active_admins,
    create_admin_account,
    get_admin_account,
    list_admin_accounts,
    set_admin_active,
    update_admin_password,
)
from core.passwords import hash_password, verify_password
from core.state import get_db_pool, verify_admin_auth

router = APIRouter(tags=["Admin Accounts"])

MIN_PASSWORD = 12
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

MESSAGES = {
    "password_changed": "Đã đổi mật khẩu. Dùng mật khẩu mới cho lần đăng nhập sau.",
    "account_created": "Đã tạo tài khoản quản trị.",
    "account_deactivated": "Đã tắt tài khoản quản trị.",
    "account_activated": "Đã bật tài khoản quản trị.",
}
ERRORS = {
    "bad_origin": "Yêu cầu không cùng nguồn gốc.",
    "current_password": "Mật khẩu hiện tại không đúng.",
    "password_policy": f"Mật khẩu mới phải có ít nhất {MIN_PASSWORD} ký tự.",
    "password_mismatch": "Xác nhận mật khẩu không khớp.",
    "same_password": "Mật khẩu mới phải khác mật khẩu hiện tại.",
    "account_exists": "Tên đăng nhập đã tồn tại.",
    "invalid_username": "Tên đăng nhập phải dài 3-32 ký tự, chỉ gồm chữ, số, dấu chấm, gạch dưới, gạch ngang.",
    "last_admin": "Không thể tắt quản trị viên đang hoạt động cuối cùng.",
    "self_deactivate": "Không thể tự tắt tài khoản của mình.",
    "not_found": "Không tìm thấy tài khoản.",
    "db_unavailable": "Cơ sở dữ liệu không khả dụng.",
}


def _require_same_origin(request: Request) -> None:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    if urlsplit(source).netloc != (request.headers.get("host") or ""):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def _env_password_ok(password: str) -> bool:
    if settings.ADMIN_PASSWORD_HASH:
        return verify_password(password, settings.ADMIN_PASSWORD_HASH)
    return secrets.compare_digest(password, settings.ADMIN_PASSWORD)


async def _audit(pool, action: str, actor: str, details: dict) -> None:
    if not pool:
        return
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,"
            "actor_type,actor_id,details) VALUES ('admin_accounts',%s,'ADMIN',%s,'ADMIN',%s,%s::jsonb)",
            (action, actor[:64], actor[:64], json.dumps(details)),
        )
        await conn.commit()


def _redirect(**params: str) -> RedirectResponse:
    query = "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())
    return RedirectResponse(url=f"/admin/accounts?{query}", status_code=303)


def _render(admin_user: str, accounts, msg: str = "", err: str = "") -> str:
    notice = ""
    if msg and msg in MESSAGES:
        notice = f'<div class="notice ok">{html.escape(MESSAGES[msg])}</div>'
    if err and err in ERRORS:
        notice = f'<div class="notice bad">{html.escape(ERRORS[err])}</div>'

    rows = []
    for account in accounts:
        username = str(account.get("username") or "")
        active = bool(account.get("is_active"))
        created = account.get("created_at")
        created_text = created.strftime("%d/%m/%Y %H:%M") if hasattr(created, "strftime") else "-"
        if username == admin_user:
            action = '<span class="muted">Tài khoản hiện tại</span>'
        elif active:
            action = (
                f'<form method="post" action="/admin/accounts/{quote(username, safe="")}/deactivate" '
                f'onsubmit="return confirm(\'Tắt tài khoản {html.escape(username)}?\')">'
                '<button class="danger" type="submit">Tắt</button></form>'
            )
        else:
            action = (
                f'<form method="post" action="/admin/accounts/{quote(username, safe="")}/activate">'
                '<button type="submit">Bật</button></form>'
            )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(username)}</code></td>"
            f'<td><span class="badge {"on" if active else "off"}">{"Hoạt động" if active else "Đã tắt"}</span></td>'
            f"<td>{created_text}</td>"
            f"<td class=\"actions\">{action}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quản lý tài khoản quản trị</title>
<style>
:root {{ --bg:#090d16; --surface:#131b2e; --border:#23314f; --text:#f8fafc; --muted:#94a3b8; --accent:#2563eb; --danger:#ef4444; --ok:#10b981; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:1.3rem; margin:0 0 4px; }}
p.sub {{ color:var(--muted); margin:0 0 20px; font-size:.85rem; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; margin-bottom:24px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; }}
.card h2 {{ font-size:.95rem; margin:0 0 12px; color:#93c5fd; }}
label {{ display:block; font-size:.8rem; color:var(--muted); margin-bottom:10px; }}
input {{ width:100%; margin-top:4px; padding:9px 10px; border-radius:8px; border:1px solid var(--border); background:#0b1220; color:var(--text); }}
button {{ padding:8px 12px; border-radius:8px; border:1px solid var(--accent); background:var(--accent); color:#fff; font-weight:600; cursor:pointer; }}
button.danger {{ background:transparent; border-color:var(--danger); color:#f87171; }}
table {{ width:100%; border-collapse:collapse; background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; }}
th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:.84rem; }}
th {{ color:var(--muted); font-weight:600; }}
tr:last-child td {{ border-bottom:none; }}
.badge {{ padding:2px 8px; border-radius:999px; font-size:.72rem; }}
.badge.on {{ background:rgba(16,185,129,.15); color:#6ee7b7; }}
.badge.off {{ background:rgba(239,68,68,.15); color:#fca5a5; }}
.actions form {{ margin:0; }}
.muted {{ color:var(--muted); font-size:.78rem; }}
.notice {{ padding:10px 12px; border-radius:8px; margin-bottom:16px; font-size:.85rem; }}
.notice.ok {{ background:rgba(16,185,129,.15); color:#a7f3d0; }}
.notice.bad {{ background:rgba(239,68,68,.15); color:#fecaca; }}
</style>
</head>
<body>
<main>
<h1>Quản lý tài khoản quản trị</h1>
<p class="sub">Đang đăng nhập: <code>{html.escape(admin_user)}</code>. Đổi mật khẩu yêu cầu nhập lại mật khẩu hiện tại.</p>
{notice}
<div class="grid">
  <section class="card">
    <h2>Đổi mật khẩu của bạn</h2>
    <form method="post" action="/admin/accounts/password">
      <label>Mật khẩu hiện tại<input type="password" name="current_password" required autocomplete="current-password"></label>
      <label>Mật khẩu mới (tối thiểu {MIN_PASSWORD} ký tự)<input type="password" name="new_password" minlength="{MIN_PASSWORD}" required autocomplete="new-password"></label>
      <label>Nhập lại mật khẩu mới<input type="password" name="confirm_password" minlength="{MIN_PASSWORD}" required autocomplete="new-password"></label>
      <button type="submit">Đổi mật khẩu</button>
    </form>
  </section>
  <section class="card">
    <h2>Tạo tài khoản quản trị</h2>
    <form method="post" action="/admin/accounts">
      <label>Tên đăng nhập<input type="text" name="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9._-]+" required autocomplete="username"></label>
      <label>Mật khẩu (tối thiểu {MIN_PASSWORD} ký tự)<input type="password" name="password" minlength="{MIN_PASSWORD}" required autocomplete="new-password"></label>
      <label>Nhập lại mật khẩu<input type="password" name="confirm_password" minlength="{MIN_PASSWORD}" required autocomplete="new-password"></label>
      <button type="submit">Tạo tài khoản</button>
    </form>
  </section>
</div>
<table>
  <thead><tr><th>Tên đăng nhập</th><th>Trạng thái</th><th>Tạo lúc</th><th></th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="4" class="muted">Chưa có tài khoản.</td></tr>'}</tbody>
</table>
</main>
</body>
</html>"""


@router.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts_page(
    request: Request,
    msg: str = "",
    err: str = "",
    admin_user: str = Depends(verify_admin_auth),
):
    pool = get_db_pool()
    accounts = await list_admin_accounts(pool)
    return HTMLResponse(_render(admin_user, accounts, msg, err))


@router.post("/admin/accounts/password")
async def change_admin_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    admin_user: str = Depends(verify_admin_auth),
):
    _require_same_origin(request)
    pool = get_db_pool()
    if not pool:
        return _redirect(err="db_unavailable")
    account = await get_admin_account(pool, admin_user)
    if account:
        current_hash = str(account.get("password_hash") or "")
        if not verify_password(current_password, current_hash):
            return _redirect(err="current_password")
    else:
        if admin_user != settings.ADMIN_USERNAME or not _env_password_ok(current_password):
            return _redirect(err="current_password")
        current_hash = ""
    if len(new_password) < MIN_PASSWORD:
        return _redirect(err="password_policy")
    if new_password != confirm_password:
        return _redirect(err="password_mismatch")
    if (account and verify_password(new_password, current_hash)) or (not account and _env_password_ok(new_password)):
        return _redirect(err="same_password")
    new_hash = hash_password(new_password)
    updated = (
        await update_admin_password(pool, admin_user, new_hash)
        if account
        else await create_admin_account(pool, admin_user, new_hash)
    )
    if not updated:
        return _redirect(err="db_unavailable")
    await _audit(pool, "ADMIN_PASSWORD_CHANGED", admin_user, {"target": admin_user})
    accounts = await list_admin_accounts(pool)
    return HTMLResponse(_render(admin_user, accounts, msg="password_changed"))


@router.post("/admin/accounts")
async def create_account(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    admin_user: str = Depends(verify_admin_auth),
):
    _require_same_origin(request)
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        return _redirect(err="invalid_username")
    if len(password) < MIN_PASSWORD:
        return _redirect(err="password_policy")
    if password != confirm_password:
        return _redirect(err="password_mismatch")
    pool = get_db_pool()
    if not pool:
        return _redirect(err="db_unavailable")
    created = await create_admin_account(pool, username, hash_password(password))
    if not created:
        return _redirect(err="account_exists")
    await _audit(pool, "ADMIN_ACCOUNT_CREATED", admin_user, {"target": username})
    return _redirect(msg="account_created")


@router.post("/admin/accounts/{username}/deactivate")
async def deactivate_account(
    username: str,
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    _require_same_origin(request)
    if username == admin_user:
        return _redirect(err="self_deactivate")
    pool = get_db_pool()
    if not pool:
        return _redirect(err="db_unavailable")
    if await count_active_admins(pool) <= 1:
        return _redirect(err="last_admin")
    if not await set_admin_active(pool, username, False):
        return _redirect(err="not_found")
    await _audit(pool, "ADMIN_ACCOUNT_DEACTIVATED", admin_user, {"target": username})
    return _redirect(msg="account_deactivated")


@router.post("/admin/accounts/{username}/activate")
async def activate_account(
    username: str,
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    _require_same_origin(request)
    pool = get_db_pool()
    if not pool:
        return _redirect(err="db_unavailable")
    if not await set_admin_active(pool, username, True):
        return _redirect(err="not_found")
    await _audit(pool, "ADMIN_ACCOUNT_ACTIVATED", admin_user, {"target": username})
    return _redirect(msg="account_activated")
