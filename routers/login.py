"""Admin login page and signed-cookie session endpoints."""
from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from core.admin_sessions import COOKIE_NAME, SESSION_TTL_SECONDS, issue
import core.state as state
from core.state import admin_from_session, require_admin_same_origin

router = APIRouter(tags=["Admin Login"])

LOGIN_PATH = "/login"
_LOGIN_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def _safe_next(value: str | None) -> str:
    target = str(value or "").strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/admin/forms"
    return target


def _page(*, next_url: str, error: str = "", username: str = "") -> str:
    error_html = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Đăng nhập quản trị | Trust Bridge</title>
<style>
:root {{ color-scheme: dark; --bg:#090d16; --panel:#111a2b; --line:#24324a; --fg:#e8eef8; --muted:#93a4bb; --blue:#2563eb; --red:#ef4444; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:radial-gradient(circle at top,#172554 0,#090d16 48%); color:var(--fg); font:15px system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; }}
.card {{ width:min(420px,calc(100vw - 32px)); background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:28px; box-shadow:0 24px 60px rgba(0,0,0,.35); }}
.brand {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; color:#93c5fd; font-weight:800; font-size:1.05rem; }}
.brand-mark {{ width:30px; height:30px; border-radius:8px; display:grid; place-items:center; background:#1d4ed8; color:white; font-weight:900; }}
h1 {{ font-size:1.35rem; margin:12px 0 6px; }}
p {{ color:var(--muted); font-size:.86rem; line-height:1.5; margin:0 0 18px; }}
label {{ display:block; color:var(--muted); font-size:.8rem; margin:12px 0; }}
input {{ width:100%; margin-top:5px; padding:10px 12px; border-radius:8px; border:1px solid var(--line); background:#0a1220; color:var(--fg); font:14px ui-monospace,monospace; }}
button {{ width:100%; margin-top:14px; padding:11px 14px; border:0; border-radius:8px; background:var(--blue); color:white; font-weight:700; cursor:pointer; }}
button:hover {{ background:#1d4ed8; }}
.error {{ background:rgba(239,68,68,.12); border:1px solid rgba(239,68,68,.45); color:#fecaca; border-radius:8px; padding:9px 11px; font-size:.83rem; margin:8px 0 4px; }}
.foot {{ color:var(--muted); font-size:.74rem; margin-top:16px; text-align:center; }}
</style>
</head>
<body>
<main class="card">
  <div class="brand"><span class="brand-mark">TB</span><span>Trust Bridge Admin</span></div>
  <h1>Đăng nhập quản trị</h1>
  <p>Dùng tài khoản quản trị đã được cấp. Phiên đăng nhập được giữ bằng cookie an toàn, không dùng hộp thoại Basic Auth.</p>
  {error_html}
  <form method="post" action="{LOGIN_PATH}">
    <input type="hidden" name="next" value="{html.escape(next_url, quote=True)}">
    <label>Tên đăng nhập<input name="username" value="{html.escape(username, quote=True)}" autocomplete="username" required maxlength="64" autofocus></label>
    <label>Mật khẩu<input type="password" name="password" autocomplete="current-password" required maxlength="1024"></label>
    <button type="submit">Đăng nhập</button>
  </form>
  <div class="foot">Quên mật khẩu? Dùng trang khôi phục hoặc liên hệ quản trị hệ thống.</div>
</main>
</body>
</html>"""


@router.get(LOGIN_PATH, response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    target = _safe_next(next)
    if await admin_from_session(request):
        return RedirectResponse(target, status_code=303)
    return HTMLResponse(_page(next_url=target), headers=_LOGIN_HEADERS)


@router.post(LOGIN_PATH, response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    require_admin_same_origin(request)
    target = _safe_next(next)
    username = username.strip()
    redis, account_bucket, ip_bucket = await state._check_admin_auth_rate_limit(request, username)
    account = await state._load_admin_account(username)
    valid = bool(
        account
        and account.get("is_active")
        and account.get("deleted_at") is None
        and state._verify_admin_password(password, str(account.get("password_hash") or ""))
    )
    if not valid:
        await state._record_admin_auth_failure(redis, account_bucket, ip_bucket)
        return HTMLResponse(
            _page(next_url=target, error="Tên đăng nhập hoặc mật khẩu không đúng.", username=username),
            status_code=401,
            headers=_LOGIN_HEADERS,
        )
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        issue(username, account.get("password_hash")),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
        path="/",
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.post("/logout")
async def logout_post():
    return await logout()
