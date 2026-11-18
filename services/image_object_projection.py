# -*- coding: utf-8 -*-
"""
Boundary adapter: project generic per-slot object results into the legacy
income shape used by DB columns, Google Drive CSV and admin dossiers.

The object definitions and their engines remain the source of truth. This
module never validates, never builds prompts, and does not assume a fixed
number of weekly slots; it projects whatever income-bearing extractions were
actually produced.
"""

from typing import Any, Dict, List, Optional

from services.image_types import parse_date_range_from_text

INCOME_KEYS = ("gross_revenue", "net_income", "discount_and_tax", "total_trips")


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit() or ch == "-")
        try:
            return int(digits) if digits not in ("", "-") else 0
        except ValueError:
            return 0
    return 0


def _extraction(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        nested = result.get("extraction")
        if isinstance(nested, dict):
            return nested
        return result
    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _period_start(extraction: Dict[str, Any]):
    raw = extraction.get("period_raw_text") or extraction.get("period") or ""
    start, _, _ = parse_date_range_from_text(str(raw))
    return start


def project_income_weeks(object_results: Optional[List[Any]]) -> Dict[str, Dict[str, Any]]:
    """Project every income-bearing extraction into `week_1..week_N` (legacy shape)."""
    candidates: List[Dict[str, Any]] = []
    for result in object_results or []:
        extraction = _extraction(result)
        if not extraction or not any(key in extraction for key in INCOME_KEYS):
            continue
        candidates.append(extraction)

    with_period = [(start, index, item) for index, item in enumerate(candidates) if (start := _period_start(item))]
    without_period = [item for item in candidates if not _period_start(item)]
    with_period.sort(key=lambda entry: (entry[0], entry[1]))
    candidates = [item for _start, _index, item in with_period] + without_period

    weeks: Dict[str, Dict[str, Any]] = {}
    for index, extraction in enumerate(candidates, start=1):
        weeks[f"week_{index}"] = {
            "gross_revenue": _as_int(extraction.get("gross_revenue")),
            "net_income": _as_int(extraction.get("net_income")),
            "total_trips": _as_int(extraction.get("total_trips")),
            "discount_and_tax": extraction.get("discount_and_tax", 0),
            "period": extraction.get("period") or extraction.get("period_raw_text") or "",
            "online_hours": extraction.get("online_hours", ""),
        }
    return weeks


def project_income_totals(weeks: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, int]:
    gross = sum(_as_int((week or {}).get("gross_revenue")) for week in (weeks or {}).values())
    net = sum(_as_int((week or {}).get("net_income")) for week in (weeks or {}).values())
    trips = sum(_as_int((week or {}).get("total_trips")) for week in (weeks or {}).values())
    return {"total_gross_revenue": gross, "total_net_income": net, "total_trips": trips}


def image_object_result(
    object_type: str,
    slot_index: Optional[int],
    slot_title: str,
    extraction: Any,
    check: Optional[Dict[str, Any]] = None,
    question_id: Optional[str] = None,
    flat_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Uniform per-slot result shape for new submissions and the editor API."""
    payload = {
        "question_id": question_id,
        "object_type": object_type,
        "slot_index": slot_index,
        "flat_index": flat_index,
        "slot_title": slot_title,
        "extraction": _extraction(extraction),
        "passed": bool((check or {}).get("passed", True)),
        "failures": list((check or {}).get("failures") or []),
        "engines": list((check or {}).get("engines") or []),
    }
    return payload
