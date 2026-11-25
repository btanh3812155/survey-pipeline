"""Shared Telegram inline-keyboard helpers."""

from __future__ import annotations

from typing import Any

COPY_SURVEY_ID_LABEL = "📋 Sao chép mã phiếu"


def survey_copy_button(submission_id: Any) -> dict[str, Any]:
    text = str(submission_id or "").strip()
    return {"text": COPY_SURVEY_ID_LABEL, "copy_text": {"text": text}}


def with_survey_copy(reply_markup: Any, submission_id: Any) -> Any:
    """Append one copy button for a submission, without breaking non-inline keyboards."""
    text = str(submission_id or "").strip()
    if not text:
        return reply_markup
    if reply_markup is None:
        return {"inline_keyboard": [[survey_copy_button(text)]]}
    if not isinstance(reply_markup, dict) or "inline_keyboard" not in reply_markup:
        return reply_markup
    rows = reply_markup.get("inline_keyboard")
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, list):
            continue
        for button in row:
            if not isinstance(button, dict):
                continue
            copy_text = button.get("copy_text")
            if isinstance(copy_text, dict) and str(copy_text.get("text") or "") == text:
                return reply_markup
    return {**reply_markup, "inline_keyboard": [*rows, [survey_copy_button(text)]]}
