"""Browser forms for administrator password recovery and recovery-email rotation."""

from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core import admin_recovery as recovery
from core.state import get_db_pool, require_admin_request_origin, verify_admin_auth

router = APIRouter(prefix="/admin/recovery", include_in_schema=False)
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}
UNAVAILABLE = "Dịch vụ khôi phục hiện không khả dụng. Hãy liên hệ quản trị hệ thống."
INVALID_CODE = "Không xác nhận được mã. Kiểm tra lại mã hoặc yêu cầu mã mới."
PUBLIC_REQUESTED = (
    "Nếu tài khoản đủ điều kiện và máy chủ email chấp nhận thư, bạn sẽ nhận được mã. "
    "Hãy giữ trang này mở. Nếu không nhận được thư, thử lại sau ít nhất một phút hoặc liên hệ quản trị hệ thống."
)


def _page(*, email: bool = False, message: str = "", challenge_id: str = "", status: int = 200) -> HTMLResponse:
    title = "Đổi email khôi phục" if email else "Khôi phục mật khẩu quản trị"
    endpoint = "/admin/recovery/email" if email else "/admin/recovery"
    request_action = endpoint if email else f"{endpoint}/request"
    confirm_action = endpoint if email else f"{endpoint}/confirm"
    request_field = (
        '<label>Email mới <input name="new_email" type="email" maxlength="254" required></label>'
        if email else '<label>Tên đăng nhập <input name="username" autocomplete="username" maxlength="64" required></label>'
    )
    code = '<input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{8}" maxlength="8" required>'
    confirm_field = (
        '<label>Mã gửi đến email mới <input name="new_code" inputmode="numeric" pattern="[0-9]{8}" maxlength="8" required></label>'
        if email else '<label>Mật khẩu mới (12–1024 ký tự ASCII in được) <input name="new_password" '
        'type="password" autocomplete="new-password" minlength="12" maxlength="1024" required></label>'
    )
    confirm_form = ""
    if recovery.CHALLENGE_ID.fullmatch(challenge_id):
        code_label = "Mã gửi đến email cũ" if email else "Mã khôi phục"
        confirm_form = f'''<form method="post" action="{confirm_action}">
<input type="hidden" name="action" value="confirm">
<input type="hidden" name="challenge_id" value="{challenge_id}">
<p><label>{code_label} {code}</label></p><p>{confirm_field}</p>
<button type="submit">Xác nhận</button></form>'''
    email_note = "<p>Bạn cần truy cập cả email khôi phục hiện tại và email mới.</p>" if email else ""
    return HTMLResponse(f'''<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title></head>
<body><main><h1>{title}</h1>{email_note}<p role="status">{message}</p>
{confirm_form}<form method="post" action="{request_action}">
<input type="hidden" name="action" value="request"><p>{request_field}</p>
<button type="submit">Yêu cầu mã</button></form></main></body></html>''', status_code=status, headers=HEADERS)


async def _form(request: Request) -> dict[str, str]:
    require_admin_request_origin(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Unsupported form encoding", headers=HEADERS)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16_384:
            raise HTTPException(413, "Form too large", headers=HEADERS)
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode("ascii"), keep_blank_values=True, encoding="utf-8", errors="strict", max_num_fields=6)
        if any(len(values) != 1 for values in fields.values()):
            raise ValueError
    except ValueError:
        # Do not echo submitted passwords or codes in framework validation errors.
        raise HTTPException(400, "Invalid form", headers=HEADERS) from None
    return {key: values[0] for key, values in fields.items()}


def _host(request: Request) -> str:
    # Uvicorn resolves proxy headers only for explicitly trusted peers.
    return request.client.host if request.client else ""


@router.get("")
async def recovery_page():
    return _page()


@router.post("/request")
async def request_reset(request: Request, background_tasks: BackgroundTasks):
    fields = await _form(request)
    pool, config = get_db_pool(), recovery.smtp_config()
    if not pool or not config:
        return _page(message=UNAVAILABLE, status=503)
    try:
        challenge_id, delivery = await recovery.issue_challenge(
            pool, _host(request), recovery.RESET, fields.get("username", "").strip(),
        )
    except recovery.DB_ERRORS:
        return _page(message=UNAVAILABLE, status=503)
    if delivery:
        background_tasks.add_task(recovery.deliver_challenge, pool, config, delivery)
    return _page(message=PUBLIC_REQUESTED, challenge_id=challenge_id, status=202)


@router.post("/confirm")
async def confirm_reset(request: Request):
    fields = await _form(request)
    challenge_id = fields.get("challenge_id", "")
    password = fields.get("new_password", "")
    if not recovery.valid_password(password):
        return _page(message="Mật khẩu cần 12–1024 ký tự ASCII in được.", challenge_id=challenge_id, status=400)
    pool = get_db_pool()
    if not pool:
        return _page(message=UNAVAILABLE, challenge_id=challenge_id, status=503)
    try:
        confirmed = await recovery.confirm_challenge(
            pool, _host(request), recovery.RESET, challenge_id, fields.get("code", ""), new_password=password,
        )
    except recovery.DB_ERRORS:
        return _page(message=UNAVAILABLE, challenge_id=challenge_id, status=503)
    if not confirmed:
        return _page(message=INVALID_CODE, challenge_id=challenge_id, status=400)
    return _page(message="Đã đổi mật khẩu. Hãy đăng nhập lại bằng mật khẩu mới.")


@router.get("/email")
async def email_page(_admin: str = Depends(verify_admin_auth)):
    return _page(email=True)


@router.post("/email")
async def change_email(request: Request, admin: str = Depends(verify_admin_auth)):
    fields = await _form(request)
    pool = get_db_pool()
    auth_hash = getattr(request.state, "admin_password_hash", None)
    challenge_id = fields.get("challenge_id", "")
    if not pool or not auth_hash:
        return _page(email=True, message=UNAVAILABLE, status=503)
    try:
        if fields.get("action") == "request":
            config = recovery.smtp_config()
            if not config:
                return _page(email=True, message=UNAVAILABLE, status=503)
            new_email = recovery.normalize_email(fields.get("new_email", ""))
            if not new_email:
                return _page(email=True, message="Địa chỉ email mới không hợp lệ.", status=400)
            challenge_id, delivery = await recovery.issue_challenge(
                pool, _host(request), recovery.EMAIL, admin, new_email=new_email, auth_hash=auth_hash,
            )
            if not delivery:
                return _page(email=True, message="Không thể tạo mã. Kiểm tra email mới hoặc thử lại sau.", status=400)
            if not await recovery.deliver_challenge(pool, config, delivery):
                return _page(email=True, message=UNAVAILABLE, status=503)
            return _page(email=True, message="Hãy nhập mã gửi đến email cũ và email mới.", challenge_id=challenge_id)
        if fields.get("action") == "confirm" and await recovery.confirm_challenge(
            pool, _host(request), recovery.EMAIL, challenge_id, fields.get("code", ""),
            new_code=fields.get("new_code", ""), username=admin, auth_hash=auth_hash,
        ):
            return _page(email=True, message="Đã đổi email khôi phục.")
    except recovery.DB_ERRORS:
        return _page(email=True, message=UNAVAILABLE, challenge_id=challenge_id, status=503)
    return _page(email=True, message=INVALID_CODE, challenge_id=challenge_id, status=400)
