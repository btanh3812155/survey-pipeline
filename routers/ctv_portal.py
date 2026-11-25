"""Authenticated, read-only CTV profile, goals and public survey catalog."""
from __future__ import annotations

import html
import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.ctv_auth import ctv_from_session
from core.quotas import SCOPE_LABELS, get_ctv_quota_evaluations
from core.state import FORM_REGISTRY, get_db_pool, refresh_form_registry_from_disk

router = APIRouter(tags=["CTV Portal"])
logger = logging.getLogger(__name__)
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def _escape(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _page(content: str) -> str:
    return """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cổng cộng tác viên | Trust Bridge</title>
<style>
:root{color-scheme:dark;--bg:#090d16;--panel:#111a2b;--line:#24324a;--fg:#e8eef8;--muted:#a7b5ca;--blue:#93c5fd}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at top,#172554 0,var(--bg) 50%);color:var(--fg);font:16px system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.55}
main{max-width:960px;margin:auto;padding:24px 16px 48px}header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:24px}.brand{font-weight:800;color:var(--blue)}h1{font-size:1.6rem;margin:4px 0}h2{font-size:1.2rem;margin:0 0 12px}h3{font-size:1rem;margin:0 0 8px}p{margin:8px 0;color:var(--muted)}section{margin-top:24px}.card{padding:20px;border:1px solid var(--line);border-radius:16px;background:var(--panel);overflow-wrap:anywhere}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px}dl{display:grid;grid-template-columns:minmax(100px,1fr) 2fr;gap:10px 16px;margin:0}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}ul{list-style:none;padding:0;margin:0}a{color:var(--blue)}button,.action{display:inline-block;min-height:44px;padding:10px 16px;border:1px solid #44618c;border-radius:8px;background:#172554;color:var(--fg);font:inherit;text-decoration:none;cursor:pointer}button:hover,.action:hover{background:#1e3a8a}a:focus-visible,button:focus-visible{outline:3px solid #bfdbfe;outline-offset:4px}.skip{position:absolute;left:16px;top:-80px;background:var(--panel);padding:12px}.skip:focus{top:12px}progress{display:block;width:100%;height:14px;margin-top:12px;accent-color:#60a5fa}.badge{display:inline-block;border:1px solid #9a803d;color:#fde68a;border-radius:6px;padding:2px 8px;font-size:.8rem;margin-bottom:8px}.note{font-size:.9rem}.count{color:var(--fg);font-weight:700}.error{border-color:#b45353}footer{margin-top:24px;color:var(--muted);font-size:.85rem}@media(max-width:480px){main{padding-top:16px}.card{padding:16px}dl{grid-template-columns:1fr;gap:4px}dd{margin-bottom:12px}}
</style></head><body><a class="skip" href="#content">Đến nội dung chính</a>
<main id="content">""" + content + "</main></body></html>"


def _goal_card(goal: dict, index: int) -> str:
    scope = SCOPE_LABELS.get(goal["scope_type"], "Mục tiêu")
    value = goal.get("scope_value")
    title = f"{scope}: {value}" if value else scope
    days = goal.get("period_days")
    period = f"{int(days)} ngày cuốn chiếu" if days and int(days) > 0 else "Toàn thời gian"
    used = int(goal["used"])
    maximum = goal.get("max_submissions")
    if maximum is None:
        usage = f"<p class=\"count\">Đã nộp: {used} phiếu</p><p>Chưa đặt số lượng mục tiêu.</p>"
    else:
        maximum = int(maximum)
        remaining = max(0, int(goal["remaining"]))
        status = "Đã đạt mục tiêu" if used >= maximum else "Đang thực hiện"
        progress = (
            f'<progress aria-labelledby="goal-{index}" max="{maximum}" value="{min(used, maximum)}">'
            f'{used}/{maximum}</progress>' if maximum > 0 else ""
        )
        usage = (
            f'<p class="count">Đã nộp: {used} / Mục tiêu: {maximum} phiếu</p>'
            f'<p>Còn để đạt mục tiêu: {remaining} phiếu · {status}</p>{progress}'
        )
    return (
        f'<li class="card"><h3 id="goal-{index}">{_escape(title)}</h3>'
        f'<p>{_escape(period)}</p>{usage}</li>'
    )


def _form_cards(code: str) -> str:
    cards = []
    for form_id, schema in sorted(FORM_REGISTRY.items(), key=lambda item: item[0]):
        settings = schema.settings
        # Archived forms are removed from the refreshed registry. Private forms
        # and response-limit closure follow the existing survey/bot convention.
        if settings.access_control != "public":
            continue
        if settings.response_limit and settings.current_responses >= settings.response_limit:
            continue
        url = f"/forms/{quote(str(form_id), safe='')}"
        if settings.ctv_ref_enabled:
            url += "?" + urlencode({'ref': code}, quote_via=quote)
            attribution = '<p class="note">Liên kết đã gắn mã CTV của bạn.</p>'
        else:
            attribution = '<p class="note">Biểu mẫu không ghi nhận mã CTV; bài nộp không tính vào tiến độ CTV.</p>'
        badge = '<span class="badge">Biểu mẫu thử nghiệm</span>' if settings.is_test_mode else ""
        cards.append(
            f'<li class="card">{badge}<h3>{_escape(schema.title)}</h3>{attribution}'
            f'<a class="action" href="{_escape(url)}">Mở khảo sát'
            f'<span class="note"> · {_escape(schema.title)}</span></a></li>'
        )
    return '<ul class="grid">' + "".join(cards) + "</ul>" if cards else '<p class="card">Hiện chưa có khảo sát công khai đang mở.</p>'


def _unavailable() -> HTMLResponse:
    return HTMLResponse(
        _page('<section class="card error" role="alert"><h1>Tạm thời không thể tải cổng CTV</h1>'
              '<p>Không thể xác nhận dữ liệu hiện tại. Vui lòng thử lại sau; không có số liệu thay thế được hiển thị.</p>'
              '<a class="action" href="/ctv">Thử lại</a></section>'),
        status_code=503,
        headers=_HEADERS,
    )


@router.get("/ctv", response_class=HTMLResponse)
async def ctv_dashboard(request: Request):
    try:
        profile = await ctv_from_session(request)
    except HTTPException as exc:
        if exc.status_code == 503:
            return _unavailable()
        raise
    if not profile or not profile.get("is_active"):
        return RedirectResponse("/ctv/login", status_code=303, headers=_HEADERS)
    try:
        goals = await get_ctv_quota_evaluations(get_db_pool(), profile["code"], strict=True)
        refresh_form_registry_from_disk()
        forms = _form_cards(str(profile["code"]))
    except Exception:
        logger.exception("CTV dashboard data unavailable")
        return _unavailable()
    goals_html = (
        '<ul class="grid">' + "".join(_goal_card(goal, index) for index, goal in enumerate(goals)) + "</ul>"
        if goals else '<p class="card">Bạn chưa được cấu hình mục tiêu.</p>'
    )
    verification = "Đã xác minh qua Telegram" if profile.get("telegram_user_id") else "Chưa liên kết Telegram"
    content = f"""<header><div><div class="brand">Trust Bridge · Cộng tác viên</div>
<h1>Cổng cộng tác viên</h1></div><form method="post" action="/ctv/logout"><button type="submit">Đăng xuất</button></form></header>
<section class="card" aria-labelledby="profile"><h2 id="profile">Thông tin của bạn</h2><dl>
<dt>Mã CTV</dt><dd>{_escape(profile['code'])}</dd>
<dt>Họ và tên</dt><dd>{_escape(profile.get('display_name') or 'Chưa cập nhật')}</dd>
<dt>Số điện thoại</dt><dd>{_escape(profile.get('phone') or 'Chưa cập nhật')}</dd>
<dt>Xác minh</dt><dd>{verification}</dd></dl></section>
<section aria-labelledby="goals"><h2 id="goals">Mục tiêu và tiến độ</h2>
<p>Mục tiêu chỉ để theo dõi tiến độ, không phải giới hạn nộp phiếu. Bạn vẫn có thể làm khảo sát đang mở sau khi đạt mục tiêu.</p>
<p class="note">Kỳ cuốn chiếu tính lùi từ thời điểm hiện tại; mỗi phiếu rời kỳ khi đủ số ngày. Toàn thời gian tính tất cả phiếu đã nộp.</p>{goals_html}</section>
<section aria-labelledby="forms"><h2 id="forms">Khảo sát đang mở</h2>
<p>Biểu mẫu có theo dõi CTV sẽ gắn mã của bạn. Biểu mẫu không theo dõi CTV được ghi chú riêng.</p>{forms}</section>
<footer>Chỉ hiển thị thông tin và tiến độ của tài khoản đang đăng nhập.</footer>"""
    return HTMLResponse(_page(content), headers=_HEADERS)
