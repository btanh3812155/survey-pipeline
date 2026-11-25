"""Read-only CTV portal behavior with isolated database and session providers."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.quotas import get_ctv_quota_evaluations


class Pool:
    def __init__(self, *, quotas=(), submissions=(), tags=(), fail_on=None):
        self.quotas = list(quotas)
        self.submissions = list(submissions)
        self.tags = list(tags)
        self.fail_on = fail_on
        self.rows = []
        self.queries = []

    def connection(self):
        return self

    def cursor(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params):
        self.queries.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("private database failure details")
        if "FROM ctv_quotas" in sql:
            self.rows = [row for row in self.quotas if row[1].upper() in {str(p).upper() for p in params} and row[6]]
        elif "FROM survey_submissions" in sql:
            self.rows = [
                (form_id, created_at) for code, form_id, created_at in self.submissions
                if code.upper() in {str(p).upper() for p in params[:2]}
                and (len(params) < 3 or created_at >= params[2])
            ]
        elif "FROM form_tags" in sql:
            self.rows = [row for row in self.tags if not params or row[0] in params[0]]
        else:
            raise AssertionError(f"Unexpected query: {sql}")

    async def fetchall(self):
        return self.rows


class Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.urls = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.extend(value for key, value in attrs if key == "href" and value.startswith("/forms/"))


def form(title="Khảo sát", *, access="public", limit=None, count=0, test=False, tracking=True):
    return SimpleNamespace(title=title, settings=SimpleNamespace(
        access_control=access, response_limit=limit, current_responses=count, is_test_mode=test,
        ctv_ref_enabled=tracking,
    ))


@pytest.fixture
def portal(monkeypatch):
    from routers import ctv_portal

    pool = Pool()
    profile = {
        "code": "OWN", "display_name": "Tên riêng", "phone": "0901111111",
        "telegram_user_id": 101, "is_active": True, "notes": "INTERNAL_SECRET",
    }
    session = AsyncMock(return_value=profile)
    monkeypatch.setattr(ctv_portal, "ctv_from_session", session)
    monkeypatch.setattr(ctv_portal, "get_db_pool", lambda: pool)
    monkeypatch.setattr(ctv_portal, "FORM_REGISTRY", {})
    refresh = lambda: False
    monkeypatch.setattr(ctv_portal, "refresh_form_registry_from_disk", refresh)
    app = FastAPI()
    app.include_router(ctv_portal.router)
    return SimpleNamespace(
        module=ctv_portal, pool=pool, profile=profile, session=session,
        client=TestClient(app), monkeypatch=monkeypatch,
    )


def assert_private(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("profile", [None, {"code": "OWN", "is_active": False}])
def test_missing_or_inactive_session_redirects_without_loading_data(portal, profile):
    portal.session.return_value = profile
    response = portal.client.get("/ctv", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ctv/login"
    assert portal.pool.queries == []
    assert_private(response)


def test_profile_is_fresh_isolated_and_escaped(portal):
    portal.profile["display_name"] = '<script>alert("x")</script>'
    response = portal.client.get("/ctv?code=OTHER&phone=0909999999")
    assert response.status_code == 200
    assert '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;' in response.text
    assert "<script>" not in response.text
    assert "0901111111" in response.text
    assert "Đã xác minh qua Telegram" in response.text
    assert "INTERNAL_SECRET" not in response.text
    assert "OTHER" not in response.text and "0909999999" not in response.text
    assert 'method="post" action="/ctv/logout"' in response.text
    assert_private(response)
    portal.session.return_value = {**portal.profile, "phone": "0902222222", "display_name": "Tên mới"}
    second = portal.client.get("/ctv")
    assert "0902222222" in second.text and "0901111111" not in second.text
    assert portal.session.await_count == 2


def test_reached_goals_do_not_close_forms_and_usage_is_personal(portal):
    now = datetime.now(timezone.utc)
    portal.pool.quotas = [
        (1, "OWN", "form", "open", 1, 7, True),
        (2, "OTHER", "global", None, 98765, None, True),
        (3, "OWN", "tag", "team", 4, 7, True),
    ]
    portal.pool.tags = [("open", "team")]
    portal.pool.submissions = [("OWN", "open", now)] * 2 + [("OTHER", "open", now)] * 5
    portal.module.FORM_REGISTRY["open"] = form("Vẫn mở")
    response = portal.client.get("/ctv")
    assert response.status_code == 200
    assert "Đã nộp: 2 / Mục tiêu: 1" in response.text
    assert "Đã nộp: 2 / Mục tiêu: 4" in response.text
    assert "Còn để đạt mục tiêu: 0" in response.text
    assert "Đã đạt mục tiêu" in response.text
    assert "7 ngày cuốn chiếu" in response.text
    assert 'max="1" value="1"' in response.text
    assert "không phải giới hạn nộp phiếu" in response.text
    assert Links(response.text).urls == ["/forms/open?ref=OWN"]
    assert "98765" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_mixed_alltime_and_rolling_goals_keep_old_submissions(strict):
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    pool = Pool(
        quotas=[(1, "OWN", "global", None, 10, None, True), (2, "OWN", "form", "a", 5, 7, True)],
        submissions=[("OWN", "a", now - timedelta(days=40)), ("OWN", "a", now - timedelta(days=1))],
    )
    evaluations = await get_ctv_quota_evaluations(pool, "OWN", now=now, strict=strict)
    assert [goal["used"] for goal in evaluations] == [2, 1]
    assert [goal["remaining"] for goal in evaluations] == [8, 4]


def test_empty_goals_are_not_an_outage(portal):
    response = portal.client.get("/ctv")
    assert response.status_code == 200
    assert "Bạn chưa được cấu hình mục tiêu." in response.text
    assert "Hiện chưa có khảo sát công khai đang mở." in response.text
    assert_private(response)


@pytest.mark.parametrize("table", ["ctv_quotas", "survey_submissions", "form_tags"])
def test_quota_query_failures_never_look_like_empty_or_zero(portal, table):
    portal.pool.quotas = [(1, "OWN", "tag", "team", 10, 7, True)]
    portal.pool.fail_on = table
    response = portal.client.get("/ctv")
    assert response.status_code == 503
    assert "Bạn chưa được cấu hình mục tiêu" not in response.text
    assert "Đã nộp: 0" not in response.text
    assert "private database failure details" not in response.text
    assert_private(response)


def test_missing_quota_database_is_not_empty(portal):
    portal.monkeypatch.setattr(portal.module, "get_db_pool", lambda: None)
    response = portal.client.get("/ctv")
    assert response.status_code == 503
    assert_private(response)


def test_session_database_outage_is_private_503(portal):
    portal.session.side_effect = HTTPException(status_code=503, detail="private outage detail")
    response = portal.client.get("/ctv")
    assert response.status_code == 503
    assert "private outage detail" not in response.text
    assert portal.pool.queries == []
    assert_private(response)


def test_catalog_refresh_eligibility_test_labels_and_encoded_local_links(portal):
    portal.profile["code"] = "OWN +/?#&"
    registry = portal.module.FORM_REGISTRY
    registry["archived"] = form("ARCHIVED_SECRET")

    def refresh():
        registry.pop("archived", None)
        registry.update({
            'open /?"&': form('<img src=x onerror="bad">', test=True),
            "private": form("PRIVATE_SECRET", access="private"),
            "closed": form("CLOSED_SECRET", limit=-1),
            "full": form("FULL_SECRET", limit=2, count=2),
            "over": form("OVER_SECRET", limit=2, count=3),
            "unlimited": form("Sans plafond", limit=0, count=999),
        })

    portal.monkeypatch.setattr(portal.module, "refresh_form_registry_from_disk", refresh)
    response = portal.client.get("/ctv", headers={"host": "dev.test"})
    assert response.status_code == 200
    urls = Links(response.text).urls
    assert len(urls) == 2
    encoded = next(url for url in urls if "open" in url)
    assert encoded.startswith("/forms/open%20%2F%3F%22%26?")
    assert "%2B%2F%3F%23%26" in encoded
    for url in urls:
        parts = urlsplit(url)
        assert not parts.scheme and not parts.netloc and not parts.fragment
        assert parse_qs(parts.query) == {"ref": [portal.profile["code"]]}
    assert "Biểu mẫu thử nghiệm" in response.text
    assert "&lt;img src=x onerror=&quot;bad&quot;&gt;" in response.text
    assert "<img" not in response.text
    for secret in ("ARCHIVED_SECRET", "PRIVATE_SECRET", "CLOSED_SECRET", "FULL_SECRET", "OVER_SECRET"):
        assert secret not in response.text


@pytest.mark.asyncio
async def test_lenient_quota_lookup_still_preserves_legacy_empty_fallback():
    assert await get_ctv_quota_evaluations(None, "OWN") == []
    assert await get_ctv_quota_evaluations(Pool(fail_on="ctv_quotas"), "OWN") == []


def test_referral_disabled_forms_are_available_without_false_attribution(portal):
    portal.module.FORM_REGISTRY.update({
        "tracked": form("Có ghi nhận CTV"),
        "untracked": form("Không ghi nhận CTV", tracking=False),
    })
    response = portal.client.get("/ctv")
    assert response.status_code == 200
    assert Links(response.text).urls == ["/forms/tracked?ref=OWN", "/forms/untracked"]
    assert "Biểu mẫu không ghi nhận mã CTV; bài nộp không tính vào tiến độ CTV." in response.text
