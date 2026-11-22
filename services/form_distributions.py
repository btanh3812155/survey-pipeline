"""Aggregate answer distributions for form-level report modules.

Distribution modules are intentionally read-only and aggregate across persisted
submissions. They are not part of the per-submission validation pipeline.
"""
from __future__ import annotations

from typing import Any, Iterable

from services.form_engine import FormModule, FormQuestion, FormSchema, QuestionType

CHOICE_TYPES = {
    QuestionType.SINGLE_CHOICE,
    QuestionType.MULTIPLE_CHOICE,
    QuestionType.DROPDOWN,
}
MULTI_TYPES = {QuestionType.MULTIPLE_CHOICE}


def _option_label(option: Any) -> str:
    if isinstance(option, dict):
        value = option.get("label") or option.get("value") or option.get("text")
        return str(value or "").strip()
    return str(option or "").strip()


def question_options(question: FormQuestion) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for option in question.options or []:
        label = _option_label(option)
        if label and label not in seen:
            seen.add(label)
            values.append(label)
    return values


def _counts_from_rows(rows: Iterable[Any], options: list[str]) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {option: 0 for option in options}
    for row in rows:
        value = str(row[0] or "").strip()
        count = int(row[1] or 0)
        if value:
            counts[value] = counts.get(value, 0) + count
    return counts, sum(counts.values())


def _values_payload(counts: dict[str, int], answered: int) -> list[dict[str, Any]]:
    payload = []
    for value, count in counts.items():
        payload.append({
            "value": value,
            "count": count,
            "percent": round((count * 100.0 / answered), 2) if answered else 0.0,
        })
    return payload


async def _single_question_counts(pool, form_id: str, question: FormQuestion) -> tuple[dict[str, int], int]:
    query = """
        SELECT COALESCE(form_data->>%s, '') AS value, count(*)::int AS count
        FROM survey_submissions
        WHERE form_id=%s
          AND form_data ? %s
          AND jsonb_typeof(form_data->%s) IN ('string', 'number', 'boolean')
          AND COALESCE(form_data->>%s, '') <> ''
        GROUP BY 1
        ORDER BY 2 DESC, 1
    """
    async with pool.connection() as conn:
        cur = await conn.execute(query, (
            question.id, form_id, question.id, question.id, question.id,
        ))
        rows = await cur.fetchall()
    counts, answered = _counts_from_rows(rows, question_options(question))
    return counts, answered


async def _multi_question_counts(pool, form_id: str, question: FormQuestion) -> tuple[dict[str, int], int]:
    # ponytail: the web form serializes multi-select as comma-joined labels.
    # Labels containing commas would need a schema-aware delimiter; arrays are
    # already accepted, so upgrading the form payload to JSON arrays removes it.
    query = """
        WITH raw AS (
            SELECT s.submission_id,
                   CASE
                       WHEN jsonb_typeof(s.form_data->%s) = 'array' THEN s.form_data->%s
                       WHEN jsonb_typeof(s.form_data->%s) = 'string'
                            THEN to_jsonb(string_to_array(s.form_data->>%s, ','))
                       ELSE '[]'::jsonb
                   END AS answers
            FROM survey_submissions s
            WHERE s.form_id=%s
        ), expanded AS (
            SELECT DISTINCT raw.submission_id, btrim(elem) AS value
            FROM raw
            CROSS JOIN LATERAL jsonb_array_elements_text(raw.answers) AS elem
        )
        SELECT value, count(*)::int AS count
        FROM expanded
        WHERE value <> ''
        GROUP BY 1
        ORDER BY 2 DESC, 1
    """
    async with pool.connection() as conn:
        cur = await conn.execute(query, (
            question.id, question.id, question.id, question.id, form_id,
        ))
        rows = await cur.fetchall()
        answered_cur = await conn.execute(
            """
            SELECT count(DISTINCT s.submission_id)::int
            FROM survey_submissions s
            WHERE s.form_id=%s
              AND s.form_data ? %s
              AND (
                  (jsonb_typeof(s.form_data->%s) = 'array' AND jsonb_array_length(s.form_data->%s) > 0)
                  OR (jsonb_typeof(s.form_data->%s) = 'string' AND btrim(s.form_data->>%s) <> '')
              )
            """,
            (form_id, question.id, question.id, question.id, question.id, question.id),
        )
        answered = int((await answered_cur.fetchone())[0] or 0)
    counts, _ = _counts_from_rows(rows, question_options(question))
    return counts, answered


async def answer_distribution(pool, form_id: str, question: FormQuestion) -> dict[str, Any]:
    if question.type == QuestionType.MULTIPLE_CHOICE:
        counts, answered = await _multi_question_counts(pool, form_id, question)
    else:
        counts, answered = await _single_question_counts(pool, form_id, question)
    return {
        "question_id": question.id,
        "title": question.title,
        "type": question.type.value,
        "answered": answered,
        "values": _values_payload(counts, answered),
    }


def distribution_questions(schema: FormSchema, module: FormModule) -> list[FormQuestion]:
    by_id = {question.id: question for question in schema.questions}
    selected: list[FormQuestion] = []
    seen: set[str] = set()
    for inp in module.inputs:
        if inp.source != "question" or not inp.question_id or inp.question_id in seen:
            continue
        question = by_id.get(inp.question_id)
        if question is None or question.type not in CHOICE_TYPES:
            continue
        seen.add(inp.question_id)
        selected.append(question)
    return selected


async def run_form_distributions(schema: FormSchema, pool) -> list[dict[str, Any]]:
    """Return one report per enabled distribution module in schema order."""
    if not pool:
        raise RuntimeError("database pool unavailable")
    reports: list[dict[str, Any]] = []
    for module in schema.settings.modules or []:
        if module.kind != "distribution" or not module.enabled:
            continue
        questions = distribution_questions(schema, module)
        reports.append({
            "uid": module.uid,
            "name": module.name,
            "distribution": [
                await answer_distribution(pool, schema.form_id, question)
                for question in questions
            ],
        })
    return reports
