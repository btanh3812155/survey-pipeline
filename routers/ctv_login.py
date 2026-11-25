"""CTV browser login: directory code or verified phone, followed by Telegram OTP."""
from __future__ import annotations

import html
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from core import ctv_auth
from core.state import require_admin_same_origin

router = APIRouter(tags=["CTV Login"])
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def _page(content: str, *, error: str = "", status_code: int = 200) -> HTMLResponse:
    notice = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đăng nhập CTV | Trust Bridge</title>
<style>
:root {{ color-scheme:dark; font:15px system-ui,-apple-system,Segoe UI,Roboto,sans-serif; color:#e8eef8; background:#090d16; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px 16px; background:radial-gradient(circle at top,#172554 0,#090d16 55%); }}
main {{ width:min(440px,100%); padding:28px; border:1px solid #24324a; border-radius:16px; background:#111a2b; }}
.brand-row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
.brand {{ color:#93c5fd; font-weight:800; }}
.lang-toggle {{ width:auto; min-height:32px; margin:0; padding:5px 10px; border:1px solid #40516b; border-radius:999px; background:#0a1220; color:#e8eef8; font-size:.78rem; }}
.lang-toggle:hover {{ background:#1b2842; }}
h1 {{ font-size:1.5rem; margin:16px 0 8px; }}
h2 {{ font-size:1rem; margin:0 0 12px; }}
p {{ color:#a9b8cc; line-height:1.6; }}
label {{ display:block; font-weight:600; }}
input,select,button {{ width:100%; font:inherit; min-height:44px; border-radius:8px; padding:10px 12px; margin-top:8px; }}
input,select {{ color:#e8eef8; background:#0a1220; border:1px solid #40516b; }}
button {{ border:0; background:#2563eb; color:white; font-weight:700; cursor:pointer; margin-top:14px; }}
button:hover {{ background:#1d4ed8; }}
:focus-visible {{ outline:3px solid #93c5fd; outline-offset:3px; }}
.separator {{ text-align:center; margin:22px 0; }}
a {{ color:#93c5fd; }}
.error {{ padding:12px; border:1px solid #dc6666; border-radius:8px; background:#3b1a25; color:#fecaca; }}
footer {{ border-top:1px solid #24324a; margin-top:24px; padding-top:16px; font-size:.85rem; }}
</style><script src="/static/ui/admin_i18n.js" defer></script></head><body><main>
<div class="brand-row"><div class="brand">Trust Bridge · Cộng tác viên</div><button type="button" class="lang-toggle" data-lang-toggle aria-label="Đổi ngôn ngữ / Toggle language">VI</button></div>
<h1>Đăng nhập CTV</h1>
{notice}{content}
<footer><a href="/login">Đăng nhập quản trị</a></footer>
</main></body></html>""", status_code=status_code, headers=_HEADERS)


def _identifiers(codes: list[str]) -> str:
    options = "".join(f'<option value="{html.escape(code, quote=True)}">{html.escape(code)}</option>' for code in codes)
    code_form = f"""<form method="post" action="/ctv/login/request">
<h2>Đăng nhập bằng mã CTV</h2>
<label>Mã CTV<select name="code" required><option value="">Chọn mã của bạn</option>{options}</select></label>
<button type="submit">Gửi OTP qua Telegram</button></form>
<p class="separator">Hoặc dùng số điện thoại</p>""" if codes else ""
    return f"""<p>Chọn mã CTV hoặc nhập số điện thoại đã xác minh với bot. Cả hai cách đều cần OTP gửi đến Telegram đã liên kết.</p>
{code_form}<form method="post" action="/ctv/login/request">
<label>Số điện thoại CTV<input type="tel" name="phone" autocomplete="tel" inputmode="tel" placeholder="0912 345 678" required maxlength="32"></label>
<button type="submit">Gửi OTP qua Telegram</button></form>
<p>Chưa liên kết Telegram? Mở liên kết bot do quản trị viên cấp và chia sẻ số liên hệ của chính bạn trước khi đăng nhập.</p>
<p>Phiên đăng nhập được giữ trong 30 ngày trên thiết bị này. Hãy đăng xuất nếu dùng thiết bị chung.</p>"""


def _verification(challenge_id: str) -> str:
    return f"""<p>OTP đã được gửi đến Telegram đã xác minh của bạn. Mã có hiệu lực trong 5 phút và chỉ dùng một lần.</p>
<form method="post" action="/ctv/login/verify">
<input type="hidden" name="challenge_id" value="{html.escape(challenge_id, quote=True)}">
<label>Mã OTP<input name="otp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" minlength="6" maxlength="6" required autofocus></label>
<button type="submit">Xác minh và đăng nhập</button></form>
<p><a href="/ctv/login?restart=1">Đổi mã CTV / số điện thoại hoặc gửi lại OTP</a></p>"""


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers=_HEADERS)


async def _login_page(*, error: str = "", status_code: int = 200) -> HTMLResponse:
    try:
        codes = await ctv_auth.list_login_codes()
    except HTTPException as exc:
        codes, error, status_code = [], str(exc.detail), exc.status_code
    return _page(_identifiers(codes), error=error, status_code=status_code)


@router.get("/ctv/login", response_class=HTMLResponse)
async def login_page(request: Request, restart: bool = False):
    if await ctv_auth.ctv_from_session(request):
        return _redirect("/ctv")
    challenge = request.cookies.get(ctv_auth.CHALLENGE_COOKIE_NAME, "")
    if not restart and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", challenge):
        return _page(_verification(challenge))
    response = await _login_page()
    if restart:
        response.delete_cookie(ctv_auth.CHALLENGE_COOKIE_NAME, path="/ctv")
    return response


@router.post("/ctv/login/request", response_class=HTMLResponse)
async def request_otp(request: Request, code: str = Form("", max_length=128), phone: str = Form("", max_length=32)):
    require_admin_same_origin(request)
    try:
        challenge = await ctv_auth.start_login(request, code=code, phone=phone)
    except HTTPException as exc:
        response = await _login_page(error=str(exc.detail), status_code=exc.status_code)
        response.headers.update(exc.headers or {})
        return response
    response = _redirect("/ctv/login")
    response.set_cookie(
        ctv_auth.CHALLENGE_COOKIE_NAME, challenge, max_age=ctv_auth.OTP_TTL_SECONDS,
        httponly=True, secure=settings.ENVIRONMENT == "production", samesite="lax", path="/ctv",
    )
    return response


@router.post("/ctv/login/verify", response_class=HTMLResponse)
async def verify_otp(request: Request, challenge_id: str = Form(..., max_length=128), otp: str = Form(..., max_length=16)):
    require_admin_same_origin(request)
    try:
        token = await ctv_auth.finish_login(request, challenge_id, otp)
    except HTTPException as exc:
        response = _page(_verification(challenge_id), error=str(exc.detail), status_code=exc.status_code)
        response.headers.update(exc.headers or {})
        return response
    response = _redirect("/ctv")
    response.set_cookie(
        ctv_auth.COOKIE_NAME, token, max_age=ctv_auth.SESSION_TTL_SECONDS,
        httponly=True, secure=settings.ENVIRONMENT == "production", samesite="lax", path="/ctv",
    )
    response.delete_cookie(ctv_auth.CHALLENGE_COOKIE_NAME, path="/ctv")
    return response


@router.post("/ctv/logout")
async def logout(request: Request):
    require_admin_same_origin(request)
    response = _redirect("/ctv/login")
    response.delete_cookie(ctv_auth.COOKIE_NAME, path="/ctv")
    response.delete_cookie(ctv_auth.CHALLENGE_COOKIE_NAME, path="/ctv")
    return response
