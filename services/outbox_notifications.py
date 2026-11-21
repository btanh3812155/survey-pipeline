"""Guarded notification delivery for the opt-in outbox worker only."""

import asyncio
import json
import re

import httpx

from config import settings
from services.outbox_types import Outcome


_TIMEOUT = 30.0
_LARK_ROOT = "https://open.larksuite.com/open-apis"
_SUBMISSION_FIELD = "Mã Phiếu (Submission ID)"
_BOT_SETTINGS = {
    "admin": "TELEGRAM_ADMIN_BOT_TOKEN",
    "driver": "TELEGRAM_DRIVER_BOT_TOKEN",
    "primary": "TELEGRAM_BOT_TOKEN",
}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _configured(value):
    return _text(value) and value.lower() not in {
        "mock", "none", "mock_tg_token", "cli_mock_id", "mock_secret"
    }


def _json_object(value):
    if not isinstance(value, dict) or any(not _text(key) for key in value):
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _reply_markup(value):
    if not _json_object(value):
        return False
    if "inline_keyboard" not in value:
        return True
    rows = value["inline_keyboard"]
    return isinstance(rows, list) and all(
        isinstance(row, list) and bool(row) and all(
            isinstance(button, dict) and _text(button.get("text")) for button in row
        ) for row in rows
    )


def _retry_after(response, data):
    parameters = data.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if type(value) is int and value > 0:
        return value
    header = response.headers.get("Retry-After", "")
    if header.isascii() and header.isdecimal() and len(header) <= 9:
        value = int(header)
        if value > 0:
            return value
    return None


async def _request(client, guard, method, url, **kwargs):
    if not await guard():
        return Outcome("UNCERTAIN", "LEASE_LOST")
    try:
        async with asyncio.timeout(_TIMEOUT):
            return await client.request(method, url, timeout=_TIMEOUT, **kwargs)
    except (httpx.TimeoutException, TimeoutError):
        return Outcome("UNCERTAIN", "PROVIDER_TIMEOUT")
    except httpx.TransportError:
        return Outcome("UNCERTAIN", "PROVIDER_TRANSPORT_ERROR")


def _body(response):
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _with_receipt(outcome, receipt):
    return Outcome(outcome.outcome, outcome.reason, receipt, outcome.retry_after)


async def _telegram(client, payload, guard):
    role = payload.get("bot_role")
    alternate = payload.get("alternate_bot_role")
    chat_id = payload.get("chat_id")
    valid_chat = (
        type(chat_id) is int and chat_id != 0 and abs(chat_id) <= 2**52
    ) or (
        isinstance(chat_id, str)
        and re.fullmatch(r"-?[1-9][0-9]{0,15}|@[A-Za-z][A-Za-z0-9_]{4,31}", chat_id)
        is not None
        and (chat_id.startswith("@") or abs(int(chat_id)) <= 2**52)
    )
    if (
        not isinstance(role, str) or role not in _BOT_SETTINGS
        or (alternate is not None and (
            not isinstance(alternate, str) or alternate not in _BOT_SETTINGS or alternate == role
        ))
        or not valid_chat or not _text(payload.get("text"))
        or len(payload["text"]) > 4096 or not _text(payload.get("message_class"))
        or ("reply_markup" in payload and not _reply_markup(payload["reply_markup"]))
        or ("parse_mode" in payload and (not isinstance(payload["parse_mode"], str) or payload["parse_mode"] not in ("HTML", "Markdown", "MarkdownV2")))
    ):
        return Outcome("TERMINAL", "INVALID_TELEGRAM_PAYLOAD")
    requested_chat = int(chat_id) if isinstance(chat_id, str) and not chat_id.startswith("@") else chat_id
    message = {"chat_id": chat_id, "text": payload["text"]}
    if "reply_markup" in payload:
        message["reply_markup"] = payload["reply_markup"]
    if "parse_mode" in payload:
        message["parse_mode"] = payload["parse_mode"]
    attempts = []
    for current_role in (role, alternate):
        if current_role is None:
            break
        receipt = {"bot_role": current_role, "chat_id": requested_chat, "attempts": attempts}
        if attempts and attempts[0].get("outcome") == "TERMINAL":
            receipt["rejected_bot_role"] = attempts[0]["bot_role"]
            receipt["rejected_error_code"] = attempts[0]["error_code"]
        token = getattr(settings, _BOT_SETTINGS[current_role], None)
        if not _configured(token):
            return Outcome("TERMINAL", "TELEGRAM_REJECTED", receipt) if attempts else Outcome("POLICY_SKIPPED", "TELEGRAM_NOT_CONFIGURED", receipt)
        token_parts = re.fullmatch(r"([1-9][0-9]{0,15}):[A-Za-z0-9_-]+", token)
        if not token_parts or int(token_parts[1]) > 2**52:
            return Outcome("TERMINAL", "INVALID_TELEGRAM_BOT_ID", receipt)
        bot_id = int(token_parts[1])
        receipt["bot_id"] = bot_id
        attempt = {"bot_role": current_role, "bot_id": bot_id, "chat_id": requested_chat}
        response = await _request(
            client, guard, "POST", f"https://api.telegram.org/bot{token}/sendMessage", json=message
        )
        if isinstance(response, Outcome):
            attempt.update(outcome=response.outcome, reason=response.reason,
                           request_started=response.reason != "LEASE_LOST")
            attempts.append(attempt)
            return _with_receipt(response, receipt)
        attempt["request_started"] = True
        attempts.append(attempt)
        data = _body(response)
        if response.status_code == 429 or (
            data.get("ok") is False and type(data.get("error_code")) is int
            and data["error_code"] == 429
        ):
            attempt.update(outcome="RETRYABLE", error_code=429)
            return Outcome("RETRYABLE", "TELEGRAM_RATE_LIMITED", receipt, _retry_after(response, data))
        if 200 <= response.status_code < 300 and data.get("ok") is True:
            result = data.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is not int or message_id <= 0:
                attempt.update(outcome="UNCERTAIN", reason="TELEGRAM_MISSING_MESSAGE_ID")
                return Outcome("UNCERTAIN", "TELEGRAM_MISSING_MESSAGE_ID", receipt)
            chat = result.get("chat")
            returned_chat = chat.get("id") if isinstance(chat, dict) else None
            username = chat.get("username") if isinstance(chat, dict) else None
            chat_matches = (
                returned_chat == requested_chat if type(requested_chat) is int else
                isinstance(username, str) and username.casefold() == requested_chat[1:].casefold()
            )
            if (type(returned_chat) is not int or returned_chat == 0 or abs(returned_chat) > 2**52
                    or not chat_matches):
                attempt.update(outcome="UNCERTAIN", reason="TELEGRAM_CHAT_UNCONFIRMED", message_id=message_id)
                return Outcome("UNCERTAIN", "TELEGRAM_CHAT_UNCONFIRMED", receipt)
            receipt.update(message_id=message_id, chat_id=returned_chat)
            attempt.update(outcome="SUCCEEDED", message_id=message_id, chat_id=returned_chat)
            return Outcome("SUCCEEDED", receipt=receipt)
        # Only an explicit API rejection permits switching identities. Neither
        # malformed replies nor server failures prove that sendMessage was rejected.
        error_code = data.get("error_code")
        if (200 <= response.status_code < 500 and data.get("ok") is False
                and type(error_code) is int and error_code in (400, 401, 403, 404)):
            attempt.update(outcome="TERMINAL", error_code=error_code)
            if current_role == role and alternate is not None:
                record_hook = getattr(guard, "__dict__", {}).get("record")
                if callable(record_hook):
                    evidence = {**receipt, "attempts": [dict(item) for item in attempts]}
                    if not await record_hook(evidence):
                        return Outcome("UNCERTAIN", "LEASE_LOST", receipt)
                continue
            return Outcome("TERMINAL", "TELEGRAM_REJECTED", receipt)
        attempt.update(outcome="UNCERTAIN", reason="TELEGRAM_UNCONFIRMED")
        return Outcome("UNCERTAIN", "TELEGRAM_UNCONFIRMED", receipt)
    return Outcome("TERMINAL", "TELEGRAM_REJECTED", {"bot_role": role, "chat_id": requested_chat, "attempts": attempts})


def _lark_result(response, *, mutation=False):
    if isinstance(response, Outcome):
        return response
    data = _body(response)
    code = data.get("code")
    if response.status_code == 429 or (
        type(code) is int and code in (99991400, 99991401, 1254290, 1254291)
    ):
        return Outcome("RETRYABLE", "LARK_RATE_LIMITED", retry_after=_retry_after(response, data))
    if response.status_code >= 500:
        return Outcome("UNCERTAIN" if mutation else "RETRYABLE", "LARK_SERVER_ERROR")
    if response.status_code == 408:
        return Outcome("UNCERTAIN", "LARK_PROVIDER_TIMEOUT")
    if 400 <= response.status_code < 500:
        return Outcome("TERMINAL", "LARK_REJECTED")
    if not 200 <= response.status_code < 300 or type(code) is not int:
        return Outcome("UNCERTAIN", "LARK_INVALID_RESPONSE")
    if code != 0:
        # Internal errors do not prove that a record mutation was rolled back.
        if code == 1254002 or 1255000 <= code <= 1255999 or code == 99991500:
            return Outcome("UNCERTAIN" if mutation else "RETRYABLE", "LARK_SERVER_ERROR")
        if mutation and not 1254000 <= code <= 1254999:
            return Outcome("UNCERTAIN", "LARK_UNCONFIRMED")
        return Outcome("TERMINAL", "LARK_REJECTED")
    return data


def _record_id(value):
    return isinstance(value, str) and re.fullmatch(r"rec[A-Za-z0-9]+", value) is not None


def _record(data):
    content = data.get("data")
    return content.get("record") if isinstance(content, dict) else None


def _identity_matches(value, identity):
    if isinstance(value, str):
        return value == identity
    # Lark search returns multiline text as typed text segments, whereas
    # record retrieval can return the same field as a plain string.
    if not isinstance(value, list) or not value:
        return False
    if any(not isinstance(part, dict) or part.get("type") != "text"
           or not isinstance(part.get("text"), str) for part in value):
        return False
    return "".join(part["text"] for part in value) == identity


async def _lark(client, event, payload, guard):
    app_token, table_id = payload.get("app_token"), payload.get("table_id")
    fields = payload.get("fields")
    identity = event.get("aggregate_id")
    record_id = payload.get("record_id")
    if (
        not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value)
                for value in (app_token, table_id))
        or not _text(identity) or not _json_object(fields)
        or fields.get(_SUBMISSION_FIELD) != identity
        or ("record_id" in payload and not _record_id(record_id))
    ):
        return Outcome("TERMINAL", "INVALID_LARK_PAYLOAD")
    app_id, secret = settings.LARK_APP_ID, settings.LARK_APP_SECRET
    if not _configured(app_id) or not _configured(secret):
        return Outcome("POLICY_SKIPPED", "LARK_NOT_CONFIGURED")
    auth = _lark_result(await _request(
        client, guard, "POST", f"{_LARK_ROOT}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": secret},
    ))
    if isinstance(auth, Outcome):
        return auth
    token = auth.get("tenant_access_token")
    if not _text(token):
        return Outcome("UNCERTAIN", "LARK_MISSING_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_LARK_ROOT}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    if record_id:
        found = _lark_result(await _request(client, guard, "GET", f"{url}/{record_id}", headers=headers))
        if isinstance(found, Outcome):
            return found
        record = _record(found)
        if (
            not isinstance(record, dict) or record.get("record_id") != record_id
            or not isinstance(record.get("fields"), dict)
            or not _identity_matches(record["fields"].get(_SUBMISSION_FIELD), identity)
        ):
            return Outcome("UNCERTAIN", "LARK_IDENTITY_UNCONFIRMED")
    else:
        found = _lark_result(await _request(
            client, guard, "POST", f"{url}/search", headers=headers,
            params={"page_size": 2},
            json={"filter": {"conjunction": "and", "conditions": [{
                "field_name": _SUBMISSION_FIELD, "operator": "is", "value": [identity]
            }]}},
        ))
        if isinstance(found, Outcome):
            return found
        data = found.get("data")
        if (
            not isinstance(data, dict) or not isinstance(data.get("items"), list)
            or type(data.get("has_more")) is not bool
        ):
            return Outcome("UNCERTAIN", "LARK_INVALID_LOOKUP")
        items = data["items"]
        if data["has_more"] or len(items) > 1:
            return Outcome("UNCERTAIN", "LARK_IDENTITY_AMBIGUOUS")
        if items:
            record = items[0]
            if (
                not isinstance(record, dict) or not _record_id(record.get("record_id"))
                or not isinstance(record.get("fields"), dict)
                or not _identity_matches(record["fields"].get(_SUBMISSION_FIELD), identity)
            ):
                return Outcome("UNCERTAIN", "LARK_IDENTITY_UNCONFIRMED")
            record_id = record["record_id"]
    operation = "update" if record_id else "create"
    receipt = {"operation": operation}
    if record_id:
        receipt["record_id"] = record_id
    result = _lark_result(await _request(
        client, guard, "PUT" if record_id else "POST",
        f"{url}/{record_id}" if record_id else url, headers=headers, json={"fields": fields},
    ), mutation=True)
    if isinstance(result, Outcome):
        return _with_receipt(result, receipt)
    record = _record(result)
    returned_id = record.get("record_id") if isinstance(record, dict) else None
    if not _record_id(returned_id) or (record_id and returned_id != record_id):
        return Outcome("UNCERTAIN", "LARK_MISSING_RECORD_ID", receipt)
    return Outcome("SUCCEEDED", receipt={"record_id": returned_id, "operation": operation})


async def execute(event: dict, submission: dict | None, conn_str: str, guard) -> Outcome:
    """Execute a snapshotted notification; never inspect or activate legacy callers."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return Outcome("TERMINAL", "INVALID_NOTIFICATION_PAYLOAD")
    policy = payload.get("policy")
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        reason = policy.get("skip_reason") if isinstance(policy, dict) else None
        return Outcome("POLICY_SKIPPED", reason if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]{1,96}", reason) else "POLICY_DISABLED")
    if not _text(policy.get("version")):
        return Outcome("TERMINAL", "MISSING_POLICY_VERSION")
    target = event.get("flow_target")
    if target not in ("TELEGRAM_NOTIFY", "LARK_SYNC"):
        return Outcome("TERMINAL", "UNSUPPORTED_NOTIFICATION_TARGET")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        if target == "TELEGRAM_NOTIFY":
            return await _telegram(client, payload, guard)
        return await _lark(client, event, payload, guard)
