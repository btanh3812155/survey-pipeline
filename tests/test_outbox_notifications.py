"""Offline protocol coverage for the guarded Wave 4A notification adapters."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from services import outbox_notifications as notifications


IDENTITY_FIELD = "Mã Phiếu (Submission ID)"


@pytest.fixture
def provider(monkeypatch):
    real_client = httpx.AsyncClient
    calls = []

    def install(handler):
        def transport(request):
            calls.append(request)
            assert all(0 < timeout <= 30 for timeout in request.extensions["timeout"].values())
            return handler(request)

        monkeypatch.setattr(
            notifications.httpx, "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs),
        )
        return calls

    monkeypatch.setattr(notifications.settings, "TELEGRAM_ADMIN_BOT_TOKEN", "123:admin-secret")
    monkeypatch.setattr(notifications.settings, "TELEGRAM_DRIVER_BOT_TOKEN", "456:driver-secret")
    monkeypatch.setattr(notifications.settings, "TELEGRAM_BOT_TOKEN", "789:primary-secret")
    monkeypatch.setattr(notifications.settings, "LARK_APP_ID", "cli_test")
    monkeypatch.setattr(notifications.settings, "LARK_APP_SECRET", "app-secret")
    return install


def telegram(**changes):
    payload = {
        "policy": {"enabled": True, "version": "v1"},
        "bot_role": "admin", "chat_id": 12345, "text": "Approved",
        "message_class": "submission.approved",
    }
    payload.update(changes)
    return {"flow_target": "TELEGRAM_NOTIFY", "aggregate_id": "VN-123", "payload": payload}


def lark(**changes):
    payload = {
        "policy": {"enabled": True, "version": "v1"},
        "app_token": "app123", "table_id": "tbl123",
        "fields": {IDENTITY_FIELD: "VN-123", "Status": "Approved"},
    }
    payload.update(changes)
    return {"flow_target": "LARK_SYNC", "aggregate_id": "VN-123", "payload": payload}


def reply(data, status=200, **kwargs):
    return httpx.Response(status, json=data, **kwargs)


def auth():
    return reply({"code": 0, "tenant_access_token": "tenant-secret"})


def lookup(items=None, **changes):
    data = {"items": [] if items is None else items, "has_more": False}
    data.update(changes)
    return reply({"code": 0, "data": data})


def record(identity="VN-123", record_id="rec123"):
    return {"record_id": record_id, "fields": {IDENTITY_FIELD: identity}}


@pytest.mark.asyncio
async def test_telegram_success_records_real_id_role_and_guard(provider):
    order = []

    async def guard():
        order.append("guard")
        return True

    def respond(request):
        order.append("http")
        assert request.url.path == "/bot123:admin-secret/sendMessage"
        assert json.loads(request.content) == {
            "chat_id": 12345, "text": "<b>Approved</b>", "reply_markup": {"inline_keyboard": []}, "parse_mode": "HTML"
        }
        return reply({"ok": True, "result": {"message_id": 77, "chat": {"id": 12345}}})

    calls = provider(respond)
    result = await notifications.execute(telegram(text="<b>Approved</b>", parse_mode="HTML", reply_markup={"inline_keyboard": []}), None, "", guard)
    assert result.outcome == "SUCCEEDED"
    assert result.receipt == {
        "message_id": 77, "bot_role": "admin", "bot_id": 123, "chat_id": 12345,
        "attempts": [{"bot_role": "admin", "bot_id": 123, "chat_id": 12345,
                      "request_started": True, "outcome": "SUCCEEDED", "message_id": 77}],
    }
    assert order == ["guard", "http"] and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "transport", "missing_id", "bool_id", "malformed", "server_error"])
async def test_telegram_uncertainty_never_tries_alternate(provider, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("token-and-PII-must-not-leak", request=request)
        if failure == "transport":
            raise httpx.ConnectError("token-and-PII-must-not-leak", request=request)
        if failure == "malformed":
            return reply(["not a response object"])
        if failure == "server_error":
            return reply({"ok": False, "error_code": 400}, status=500)
        return reply({"ok": True, "result": {"message_id": True} if failure == "bool_id" else {}})

    calls = provider(respond)
    guard = AsyncMock(return_value=True)
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", guard)
    assert result.outcome == "UNCERTAIN"
    assert result.receipt["bot_role"] == "admin"
    assert result.receipt["bot_id"] == 123 and result.receipt["chat_id"] == 12345
    assert len(result.receipt["attempts"]) == 1
    assert result.receipt["attempts"][0]["outcome"] == "UNCERTAIN"
    assert result.receipt["attempts"][0]["request_started"] is True
    assert "token-and-PII" not in repr(result)
    assert len(calls) == guard.await_count == 1


@pytest.mark.asyncio
async def test_telegram_definitive_rejection_allows_guarded_alternate(provider):
    def respond(request):
        if "admin-secret" in request.url.path:
            return reply({"ok": False, "error_code": 403, "description": "private recipient"}, 403)
        return reply({"ok": True, "result": {"message_id": 88, "chat": {"id": 12345}}})

    calls = provider(respond)
    guard = AsyncMock(return_value=True)
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", guard)
    assert result.outcome == "SUCCEEDED"
    assert result.receipt == {
        "bot_role": "driver", "bot_id": 456, "chat_id": 12345, "message_id": 88,
        "rejected_bot_role": "admin", "rejected_error_code": 403,
        "attempts": [
            {"bot_role": "admin", "bot_id": 123, "chat_id": 12345,
             "request_started": True, "outcome": "TERMINAL", "error_code": 403},
            {"bot_role": "driver", "bot_id": 456, "chat_id": 12345,
             "request_started": True, "outcome": "SUCCEEDED", "message_id": 88},
        ],
    }
    assert len(calls) == guard.await_count == 2
    assert "private recipient" not in repr(result)


@pytest.mark.asyncio
async def test_alternate_is_not_sent_after_lease_loss(provider):
    calls = provider(lambda request: reply({"ok": False, "error_code": 403}, 403))
    guard = AsyncMock(side_effect=[True, False])
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", guard)
    assert (result.outcome, result.reason) == ("UNCERTAIN", "LEASE_LOST")
    assert result.receipt["bot_role"] == "driver" and result.receipt["bot_id"] == 456
    assert result.receipt["rejected_bot_role"] == "admin" and result.receipt["rejected_error_code"] == 403
    assert result.receipt["attempts"] == [
        {"bot_role": "admin", "bot_id": 123, "chat_id": 12345,
         "request_started": True, "outcome": "TERMINAL", "error_code": 403},
        {"bot_role": "driver", "bot_id": 456, "chat_id": 12345,
         "request_started": False, "outcome": "UNCERTAIN", "reason": "LEASE_LOST"},
    ]
    assert len(calls) == 1 and guard.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("header,parameters,expected", [("91", {}, 91), ("99", {"retry_after": 27}, 27), ("bad", {"retry_after": True}, None)])
async def test_telegram_429_returns_retry_after_without_retry_or_alternate(provider, header, parameters, expected):
    calls = provider(lambda request: reply(
        {"ok": False, "error_code": 429, "parameters": parameters}, 429, headers={"Retry-After": header}
    ))
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", AsyncMock(return_value=True))
    assert result.outcome == "RETRYABLE" and result.retry_after == expected
    assert len(calls) == 1 and result.receipt["bot_role"] == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [telegram, lark])
@pytest.mark.parametrize("policy", [None, {}, {"enabled": False}, {"enabled": 1}])
async def test_disabled_policy_never_calls_provider(provider, factory, policy):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    event = factory()
    if policy is None:
        event["payload"].pop("policy")
    else:
        event["payload"]["policy"] = policy
    guard = AsyncMock(return_value=True)
    result = await notifications.execute(event, None, "", guard)
    assert result.outcome == "POLICY_SKIPPED"
    assert not calls and guard.await_count == 0


@pytest.mark.asyncio
async def test_unconfigured_role_is_explicit_skip_without_primary_fallback(provider, monkeypatch):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    monkeypatch.setattr(notifications.settings, "TELEGRAM_ADMIN_BOT_TOKEN", None)
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("POLICY_SKIPPED", "TELEGRAM_NOT_CONFIGURED")
    assert result.receipt == {"bot_role": "admin", "chat_id": 12345, "attempts": []} and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"bot_role": []}, {"alternate_bot_role": {}}, {"chat_id": True}, {"chat_id": []},
    {"chat_id": 0}, {"chat_id": ""}, {"text": {}}, {"text": " "},
    {"message_class": None}, {"reply_markup": []}, {"reply_markup": {"bad": float("nan")}},
])
async def test_telegram_malformed_intent_is_terminal_without_io(provider, changes):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    result = await notifications.execute(telegram(**changes), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("TERMINAL", "INVALID_TELEGRAM_PAYLOAD")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("proven,rich", [(False, False), (False, True), (True, False), (True, True)])
async def test_lark_existing_identity_updates_and_records_receipt(provider, proven, rich):
    existing = record()
    if rich:
        existing["fields"][IDENTITY_FIELD] = [{"type": "text", "text": "VN-123"}]
    order = []

    async def guard():
        order.append("guard")
        return True

    def respond(request):
        order.append("http")
        if "/auth/" in request.url.path:
            assert json.loads(request.content) == {"app_id": "cli_test", "app_secret": "app-secret"}
            return auth()
        assert request.headers["Authorization"] == "Bearer tenant-secret"
        if request.method == "GET":
            assert proven and request.url.path.endswith("/rec123")
            return reply({"code": 0, "data": {"record": existing}})
        if request.url.path.endswith("/search"):
            assert not proven and request.url.params["page_size"] == "2"
            condition = json.loads(request.content)["filter"]["conditions"][0]
            assert condition == {"field_name": IDENTITY_FIELD, "operator": "is", "value": ["VN-123"]}
            return lookup([existing])
        assert request.method == "PUT" and request.url.path.endswith("/rec123")
        assert json.loads(request.content)["fields"][IDENTITY_FIELD] == "VN-123"
        return reply({"code": 0, "data": {"record": record()}})

    calls = provider(respond)
    event = lark(record_id="rec123") if proven else lark()
    result = await notifications.execute(event, None, "", guard)
    assert result.outcome == "SUCCEEDED"
    assert result.receipt == {"record_id": "rec123", "operation": "update"}
    assert order == ["guard", "http"] * 3 and len(calls) == 3


@pytest.mark.asyncio
async def test_lark_empty_complete_lookup_creates_once_with_real_id(provider):
    def respond(request):
        if "/auth/" in request.url.path:
            return auth()
        if request.url.path.endswith("/search"):
            return lookup()
        assert request.method == "POST" and request.url.path.endswith("/records")
        return reply({"code": 0, "data": {"record": {"record_id": "recCreated"}}})

    calls = provider(respond)
    guard = AsyncMock(return_value=True)
    result = await notifications.execute(lark(), None, "", guard)
    assert result.outcome == "SUCCEEDED"
    assert result.receipt == {"record_id": "recCreated", "operation": "create"}
    assert len(calls) == guard.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "missing_id", "malformed_id", "malformed_body", "server_error", "internal_error"])
async def test_lark_ambiguous_creation_is_uncertain_and_never_replayed(provider, failure):
    def respond(request):
        if "/auth/" in request.url.path:
            return auth()
        if request.url.path.endswith("/search"):
            return lookup()
        if failure == "timeout":
            raise httpx.ReadTimeout("tenant-secret private body", request=request)
        if failure == "server_error":
            return reply({"code": 0}, 503)
        if failure == "internal_error":
            return reply({"code": 1254002, "msg": "tenant-secret private body"})
        if failure == "malformed_body":
            return reply(["private body"])
        return reply({"code": 0, "data": {"record": {} if failure == "missing_id" else {"record_id": "../x"}}})

    calls = provider(respond)
    result = await notifications.execute(lark(), None, "", AsyncMock(return_value=True))
    assert result.outcome == "UNCERTAIN" and result.receipt == {"operation": "create"}
    assert "tenant-secret" not in repr(result) and "private body" not in repr(result)
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "multiple", "has_more", "missing_has_more", "wrong_identity", "bad_record", "bad_body"])
async def test_lark_unproven_lookup_never_creates(provider, failure):
    def respond(request):
        if "/auth/" in request.url.path:
            return auth()
        assert request.url.path.endswith("/search")
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "multiple":
            return lookup([record(), record(record_id="rec456")])
        if failure == "has_more":
            return lookup(has_more=True)
        if failure == "missing_has_more":
            return reply({"code": 0, "data": {"items": []}})
        if failure == "wrong_identity":
            return lookup([record(identity="VN-other")])
        if failure == "bad_record":
            return lookup([None])
        return reply({"code": 0, "data": []})

    calls = provider(respond)
    result = await notifications.execute(lark(), None, "", AsyncMock(return_value=True))
    assert result.outcome == "UNCERTAIN" and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [0, 1, 2])
async def test_lark_guard_fences_every_http_operation(provider, allowed):
    def respond(request):
        return auth() if "/auth/" in request.url.path else lookup()

    calls = provider(respond)
    guard = AsyncMock(side_effect=[True] * allowed + [False])
    result = await notifications.execute(lark(), None, "", guard)
    assert (result.outcome, result.reason) == ("UNCERTAIN", "LEASE_LOST")
    assert len(calls) == allowed and guard.await_count == allowed + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [(1254290, 200), (1254291, 200), (99991400, 200), (0, 429)])
async def test_lark_rate_limit_returns_sanitized_retry_after(provider, code, status):
    calls = provider(lambda request: reply({"code": code, "msg": "app-secret"}, status, headers={"Retry-After": "75"}))
    result = await notifications.execute(lark(), None, "", AsyncMock(return_value=True))
    assert result.outcome == "RETRYABLE" and result.retry_after == 75
    assert "app-secret" not in repr(result) and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"app_token": "../app"}, {"table_id": []}, {"fields": []},
    {"fields": {"Status": "Approved"}}, {"fields": {IDENTITY_FIELD: "other"}},
    {"fields": {IDENTITY_FIELD: "VN-123", "Status": float("nan")}},
    {"record_id": "../rec123"}, {"record_id": True}, {"record_id": None},
])
async def test_lark_malformed_fields_are_terminal_without_provider_io(provider, changes):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    result = await notifications.execute(lark(**changes), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("TERMINAL", "INVALID_LARK_PAYLOAD")
    assert not calls


@pytest.mark.asyncio
async def test_lark_mock_credentials_explicitly_skip(provider, monkeypatch):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    monkeypatch.setattr(notifications.settings, "LARK_APP_ID", "cli_mock_id")
    result = await notifications.execute(lark(), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("POLICY_SKIPPED", "LARK_NOT_CONFIGURED")
    assert not calls


@pytest.mark.asyncio
async def test_definitive_telegram_rejection_without_alternate_is_terminal(provider):
    calls = provider(lambda request: reply({"ok": False, "error_code": 403, "description": "private"}, 403))
    result = await notifications.execute(telegram(), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("TERMINAL", "TELEGRAM_REJECTED")
    assert result.receipt == {
        "bot_role": "admin", "bot_id": 123, "chat_id": 12345,
        "attempts": [{"bot_role": "admin", "bot_id": 123, "chat_id": 12345,
                      "request_started": True, "outcome": "TERMINAL", "error_code": 403}],
    }
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("markup", [{"inline_keyboard": "bad"}, {"inline_keyboard": [None]}, {"inline_keyboard": [[{"text": 1}]]}])
async def test_malformed_inline_markup_never_sends(provider, markup):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    result = await notifications.execute(telegram(reply_markup=markup), None, "", AsyncMock(return_value=True))
    assert result.outcome == "TERMINAL" and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "wrong_identity", "not_found"])
async def test_lark_proven_record_failure_never_falls_back_to_create(provider, failure):
    def respond(request):
        if "/auth/" in request.url.path:
            return auth()
        assert request.method == "GET" and request.url.path.endswith("/rec123")
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "not_found":
            return reply({"code": 1254043})
        return reply({"code": 0, "data": {"record": record(identity="other")}})

    calls = provider(respond)
    result = await notifications.execute(lark(record_id="rec123"), None, "", AsyncMock(return_value=True))
    assert result.outcome == ("TERMINAL" if failure == "not_found" else "UNCERTAIN")
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "missing_id", "different_id", "unknown_code"])
async def test_lark_unconfirmed_update_retains_proven_record_receipt(provider, failure):
    def respond(request):
        if "/auth/" in request.url.path:
            return auth()
        if request.method == "GET":
            return reply({"code": 0, "data": {"record": record()}})
        assert request.method == "PUT"
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "unknown_code":
            return reply({"code": 123456789, "msg": "private"})
        returned = {} if failure == "missing_id" else {"record_id": "recDifferent"}
        return reply({"code": 0, "data": {"record": returned}})

    calls = provider(respond)
    result = await notifications.execute(lark(record_id="rec123"), None, "", AsyncMock(return_value=True))
    assert result.outcome == "UNCERTAIN"
    assert result.receipt == {"operation": "update", "record_id": "rec123"}
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"code": 0}, {"code": True, "tenant_access_token": "secret"}, []])
async def test_lark_unconfirmed_auth_never_looks_up_or_mutates(provider, body):
    calls = provider(lambda request: reply(body))
    result = await notifications.execute(lark(), None, "", AsyncMock(return_value=True))
    assert result.outcome == "UNCERTAIN" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [telegram, lark])
async def test_enabled_policy_requires_durable_version(provider, factory):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    event = factory(policy={"enabled": True})
    result = await notifications.execute(event, None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("TERMINAL", "MISSING_POLICY_VERSION")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [None, {}, {"id": True}, {"id": 0}, {"id": 98765}, {"id": "12345"}, {"id": 2**53}])
async def test_telegram_unvalidated_returned_chat_is_uncertain_without_alternate(provider, chat):
    calls = provider(lambda request: reply({"ok": True, "result": {"message_id": 77, "chat": chat}}))
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("UNCERTAIN", "TELEGRAM_CHAT_UNCONFIRMED")
    assert result.receipt["bot_id"] == 123 and result.receipt["chat_id"] == 12345
    assert result.receipt["attempts"][0]["message_id"] == 77
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,returned", [
    ("12345", {"id": 12345}),
    ("@sample_channel", {"id": -10012345, "username": "Sample_Channel"}),
])
async def test_telegram_chat_receipt_uses_validated_numeric_identity(provider, requested, returned):
    calls = provider(lambda request: reply({"ok": True, "result": {"message_id": 77, "chat": returned}}))
    result = await notifications.execute(telegram(chat_id=requested), None, "", AsyncMock(return_value=True))
    assert result.outcome == "SUCCEEDED"
    assert result.receipt["bot_id"] == 123 and result.receipt["chat_id"] == returned["id"]
    assert result.receipt["attempts"][0]["chat_id"] == returned["id"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_telegram_channel_username_must_match_response(provider):
    calls = provider(lambda request: reply({"ok": True, "result": {
        "message_id": 77, "chat": {"id": -10012345, "username": "different_channel"}
    }}))
    result = await notifications.execute(telegram(chat_id="@sample_channel"), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("UNCERTAIN", "TELEGRAM_CHAT_UNCONFIRMED")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["missing-prefix", "0:secret", "bad:secret", "9007199254740992:secret", "123:secret/path"])
async def test_telegram_invalid_bot_identity_never_calls_provider(provider, monkeypatch, token):
    calls = provider(lambda request: pytest.fail("Provider operation forbidden"))
    monkeypatch.setattr(notifications.settings, "TELEGRAM_ADMIN_BOT_TOKEN", token)
    result = await notifications.execute(telegram(), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("TERMINAL", "INVALID_TELEGRAM_BOT_ID")
    assert "bot_id" not in result.receipt and not calls
    assert token not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [True, False])
async def test_telegram_persists_rejection_before_alternate_and_fences_failed_write(provider, recorded):
    order = []
    evidence = []

    async def guard():
        order.append("guard")
        return True

    async def record_hook(receipt):
        order.append("record")
        evidence.append(receipt)
        return recorded

    guard.record = record_hook

    def respond(request):
        if "admin-secret" in request.url.path:
            order.append("reject")
            return reply({"ok": False, "error_code": 403}, 403)
        order.append("alternate")
        return reply({"ok": True, "result": {"message_id": 88, "chat": {"id": 12345}}})

    calls = provider(respond)
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", guard)
    assert evidence == [{
        "bot_role": "admin", "bot_id": 123, "chat_id": 12345,
        "attempts": [{"bot_role": "admin", "bot_id": 123, "chat_id": 12345,
                      "request_started": True, "outcome": "TERMINAL", "error_code": 403}],
    }]
    if recorded:
        assert result.outcome == "SUCCEEDED" and len(calls) == 2
        assert order == ["guard", "reject", "record", "guard", "alternate"]
        assert len(result.receipt["attempts"]) == 2
    else:
        assert (result.outcome, result.reason) == ("UNCERTAIN", "LEASE_LOST")
        assert len(calls) == 1 and order == ["guard", "reject", "record"]
        assert result.receipt == evidence[0]


@pytest.mark.asyncio
async def test_autogenerated_mock_record_attribute_is_not_used(provider):
    def respond(request):
        if "admin-secret" in request.url.path:
            return reply({"ok": False, "error_code": 403}, 403)
        return reply({"ok": True, "result": {"message_id": 88, "chat": {"id": 12345}}})

    calls = provider(respond)
    guard = AsyncMock(return_value=True)
    autogenerated_record = guard.record
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", guard)
    assert result.outcome == "SUCCEEDED" and len(calls) == 2
    autogenerated_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_primary_with_unconfigured_alternate_is_failed_not_skipped(provider, monkeypatch):
    calls = provider(lambda request: reply({"ok": False, "error_code": 403}, 403))
    monkeypatch.setattr(notifications.settings, "TELEGRAM_DRIVER_BOT_TOKEN", "mock_tg_token")
    result = await notifications.execute(telegram(alternate_bot_role="driver"), None, "", AsyncMock(return_value=True))
    assert (result.outcome, result.reason) == ("TERMINAL", "TELEGRAM_REJECTED")
    assert len(calls) == 1 and result.receipt["attempts"][0]["error_code"] == 403
