"""CTV directory disclosure, escaping, and attributed-link eligibility contracts."""

from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routers import admin_ctvs


class PageElements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.selects = {}
        self.select = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "select":
            self.select = attrs.get("id")
            self.selects[self.select] = []
        elif tag == "option" and self.select:
            self.selects[self.select].append(attrs.get("value"))

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def row(code="ACTIVE", **overrides):
    return {"code": code, "display_name": 'A "quoted" <name>', "phone": "0901234567",
            "notes": '<script>alert("notes")</script>', "telegram_user_id": 1234,
            "is_active": True, "submission_count": 7, "source": "db", **overrides}


def test_directory_uses_single_edit_delete_forms_and_svg_actions():
    text = admin_ctvs._render_page([row()])
    page = PageElements(text)
    forms = [attrs for tag, attrs in page.tags if tag == "form"]
    assert sum(attrs.get("action") == "/admin/ctvs/ACTIVE" for attrs in forms) == 1
    assert sum(attrs.get("action") == "/admin/ctvs/ACTIVE/delete" for attrs in forms) == 1
    assert not any("/toggle" in attrs.get("action", "") for attrs in forms)
    assert '<th>Thông tin</th>' not in text
    assert '<details class="ctv-details"' in text
    assert '<summary><code>ACTIVE</code></summary>' in text
    assert '<dt>Telegram</dt><dd>1234</dd>' in text
    assert '<dt>Bài nộp</dt><dd>7' in text
    assert '<script>alert("notes")</script>' not in text
    assert '&lt;script&gt;' in text and '&quot;quoted&quot;' in text
    assert any(tag == "svg" and attrs.get("aria-hidden") == "true" for tag, attrs in page.tags)
    assert any(tag == "button" and attrs.get("aria-label") == "Xóa CTV ACTIVE" for tag, attrs in page.tags)
    assert any(tag == "input" and attrs.get("name") == "is_active" for tag, attrs in page.tags)


def test_generator_only_offers_confirmed_active_directory_entries():
    text = admin_ctvs._render_page([
        row(), row("OFF", is_active=False), row("CACHED", source="cache"),
        row("STATIC", source="static"),
    ], forms=[('form /"x', '<Open form>')])
    page = PageElements(text)
    assert page.selects["ctv-link-code"] == ["", "ACTIVE"]
    assert page.selects["ctv-link-form-id"] == ["", 'form /"x']
    assert '&lt;Open form&gt;' in text
    for action in ("copy-link", "share-link", "copy-image", "share-image", "download-image"):
        assert any(tag == "button" and attrs.get("id") == f"ctv-{action}" and "disabled" in attrs
                   for tag, attrs in page.tags)


@pytest.mark.asyncio
async def test_directory_refreshes_registry_before_filtering_attributed_forms(monkeypatch):
    def schema(title, **settings):
        return SimpleNamespace(title=title, settings=SimpleNamespace(
            **{"access_control": "public", "ctv_ref_enabled": True,
               "response_limit": None, "current_responses": 0, **settings}))

    registry = {"archived": schema("Archived")}

    def refresh():
        registry.clear()
        registry.update({
            'open /"x': schema("Open"),
            "private": schema("Private", access_control="private"),
            "disabled": schema("Disabled", ctv_ref_enabled=False),
            "closed": schema("Closed", response_limit=2, current_responses=2),
            "over": schema("Over", response_limit=2, current_responses=3),
            "remaining": schema("Remaining", response_limit=2, current_responses=1),
        })

    monkeypatch.setattr(admin_ctvs, "FORM_REGISTRY", registry)
    monkeypatch.setattr(admin_ctvs, "refresh_form_registry_from_disk", refresh)
    monkeypatch.setattr(admin_ctvs, "get_db_pool", lambda: object())
    monkeypatch.setattr(admin_ctvs, "_load_directory_rows", AsyncMock(return_value=[row()]))
    monkeypatch.setattr(admin_ctvs, "get_ctv_quota_evaluations", AsyncMock(return_value=[]))
    response = await admin_ctvs.admin_ctv_directory(admin_user="admin")
    page = PageElements(response.body.decode())
    assert page.selects["ctv-link-form-id"] == ["", 'open /"x', "remaining"]
