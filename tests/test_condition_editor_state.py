"""State-level regression tests for the condition builder in form_editor.html.

Runs the editor's real inline JS in a stubbed DOM (node) and exercises the
functions the UI calls: moving questions, rendering condition rows, pre-save
validation, and the exact payload saveForm builds.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType
from services.form_manager_html import render_form_editor_html


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _schema() -> FormSchema:
    return FormSchema(
        form_id="cond_editor_state",
        title="Condition editor state",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="q1", title="Câu 1", type=QuestionType.SINGLE_CHOICE, options=["A", "B"]
            ),
            FormQuestion(
                id="q2", title="Câu 2", type=QuestionType.SINGLE_CHOICE, options=["C", "D"]
            ),
            FormQuestion(
                id="q3",
                title="Câu 3",
                type=QuestionType.TEXT,
                condition_groups=[[
                    {"depends_on_question_id": "q1", "operator": "equals", "expected_value": ["A"]},
                    {"depends_on_question_id": "q2", "operator": "not_empty", "expected_value": None},
                ]],
            ),
        ],
    )


def _run_editor_js(body_js: str) -> None:
    html = render_form_editor_html(_schema(), is_new=False)
    match = re.search(r"<script>([\s\S]*?)</script>", html)
    assert match, "inline editor script not found"
    script = match.group(1)
    harness = (
        "const vm = require('vm');\n"
        f"const script = {json.dumps(script, ensure_ascii=True)};\n"
        "function fakeEl() {\n"
        "  return {\n"
        "    innerHTML: '', style: {}, value: '', checked: false, textContent: '',\n"
        "    classList: { add() {}, remove() {}, contains() { return false; } },\n"
        "    appendChild() {}, querySelectorAll() { return []; }, focus() {},\n"
        "  };\n"
        "}\n"
        "const els = {};\n"
        "const alerts = [];\n"
        "const fetchCalls = [];\n"
        "const sandbox = {\n"
        "  console, setTimeout, clearTimeout,\n"
        "  JSON, Math, Date, Object, Array, String, Number, Boolean, Set, Map, Promise, RegExp, Error,\n"
        "  localStorage: { getItem() { return null; }, setItem() {} },\n"
        "  alert(msg) { alerts.push(String(msg)); },\n"
        "  fetch: async (url, opts) => {\n"
        "    fetchCalls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });\n"
        "    return { ok: true, status: 200, json: async () => ({}), text: async () => '' };\n"
        "  },\n"
        "  document: {\n"
        "    getElementById(id) { return els[id] || (els[id] = fakeEl()); },\n"
        "    querySelectorAll() { return []; },\n"
        "    createElement() { return fakeEl(); },\n"
        "    addEventListener() {},\n"
        "    title: '',\n"
        "  },\n"
        "};\n"
        "sandbox.window = { addEventListener() {}, IMAGE_OBJECTS: [], CHECK_ENGINES: [], IMAGE_OBJECTS_ERROR: '', location: { href: '' } };\n"
        "vm.createContext(sandbox);\n"
        "vm.runInContext(script + '\\n;globalThis.__t = { questions, normalizeConditionals, initializeConditionUids, "
        "hydrateEditorFromSchema, renderQuestions, saveForm, moveQuestion, addConditionalGroup, "
        "addConditionalRow, removeConditionalRow, removeConditionalGroup, setConditionalParent, "
        "setConditionalOperator, setConditionalValuesText, updateQuestionId, removeQuestion, "
        "renderConditionalBuilderHtml, collectInvalidConditionRefs };', sandbox);\n"
        "const t = sandbox.__t;\n"
        "function boot() { t.normalizeConditionals(); t.initializeConditionUids(); t.hydrateEditorFromSchema(); t.renderQuestions(); }\n"
        "function check(cond, msg) { if (!cond) throw new Error(msg); }\n"
        "boot();\n"
        "(async () => {\n"
        + body_js
        + "\n})().catch(e => { console.error(e && e.stack || e); process.exit(1); });\n"
    )
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(harness)
        proc = subprocess.run([shutil.which("node"), path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_forward_reference_is_shown_and_blocks_save_with_clear_error():
    _run_editor_js(r"""
check(t.questions.length === 3, 'unexpected question count');
check(t.collectInvalidConditionRefs().length === 0, 'initial schema should be valid');

t.moveQuestion(2, -1); // q3 now sits before its parent q2
const errors = t.collectInvalidConditionRefs();
check(errors.length === 1, 'forward ref not detected: ' + JSON.stringify(errors));
check(errors[0].message.includes("không nằm trước"), 'unclear error: ' + errors[0].message);

const idx = t.questions.findIndex(q => q.id === 'q3');
const builder = t.renderConditionalBuilderHtml(t.questions[idx], idx);
check(builder.includes('SAU câu này - không hợp lệ'), 'UI must show the real forward parent, got: ' + builder.slice(0, 400));

await t.saveForm();
check(fetchCalls.length === 0, 'invalid conditions must not POST');
check(alerts.length === 1 && alerts[0].includes('Không thể lưu'), 'expected pre-save error, got: ' + alerts.join(' | '));
check(alerts[0].includes('không nằm trước'), 'error should name the forward reference: ' + alerts[0]);
""")


def test_reorder_back_allows_save_and_preserves_exact_groups():
    _run_editor_js(r"""
const expected = [[
  { depends_on_question_id: 'q1', operator: 'equals', expected_value: ['A'] },
  { depends_on_question_id: 'q2', operator: 'not_empty', expected_value: null },
]];

t.moveQuestion(2, -1);
check(t.collectInvalidConditionRefs().length === 1, 'expected one invalid ref');
t.moveQuestion(1, 1); // restore q1, q2, q3
check(t.collectInvalidConditionRefs().length === 0, 'restored order must validate');

const stateBefore = JSON.stringify(t.questions.find(q => q.id === 'q3').condition_groups);
await t.saveForm();
check(fetchCalls.length === 1, 'valid conditions should POST exactly once');
const payload = fetchCalls[0].body;
const saved = payload.questions.find(q => q.id === 'q3').condition_groups;
check(JSON.stringify(saved) === JSON.stringify(expected), 'saved groups changed: ' + JSON.stringify(saved));
check(!payload.questions[0].condition_groups && !payload.questions[1].condition_groups,
  'unrelated questions must not gain condition groups');
check(JSON.stringify(t.questions.find(q => q.id === 'q3').condition_groups) === stateBefore,
  'save mutated editor state');
""")


def test_add_and_edit_groups_round_trip_through_save_payload():
    _run_editor_js(r"""
// q3 gets an extra OR group with two rows
t.addConditionalGroup(2);
t.addConditionalRow(2, 1);
t.setConditionalParent(2, 1, 1, t.questions[0].__uid);
t.setConditionalOperator(2, 1, 1, 'equals');
t.setConditionalValuesText(2, 1, 1, 'B');
check(t.collectInvalidConditionRefs().length === 0, 'edited groups should be valid');

await t.saveForm();
check(fetchCalls.length === 1, 'save should POST');
const saved = fetchCalls[0].body.questions.find(q => q.id === 'q3').condition_groups;
check(saved.length === 2, 'new OR group dropped: ' + JSON.stringify(saved));
check(JSON.stringify(saved[0]) === JSON.stringify([
  { depends_on_question_id: 'q1', operator: 'equals', expected_value: ['A'] },
  { depends_on_question_id: 'q2', operator: 'not_empty', expected_value: null },
]), 'existing group mutated: ' + JSON.stringify(saved[0]));
check(JSON.stringify(saved[1]) === JSON.stringify([
  { depends_on_question_id: 'q2', operator: 'equals', expected_value: ['C'] },
  { depends_on_question_id: 'q1', operator: 'equals', expected_value: ['B'] },
]), 'new group saved incorrectly: ' + JSON.stringify(saved[1]));

// removing the rows again must drop the group, not leave an empty one
t.removeConditionalRow(2, 1, 1);
t.removeConditionalRow(2, 1, 0);
check(t.questions[2].condition_groups.length === 1, 'empty group should be removed');
""")


def test_removed_question_cleans_refs_even_with_empty_id():
    _run_editor_js(r"""
const q2Uid = t.questions[1].__uid;
t.updateQuestionId(1, ''); // admin cleared the visible id before deleting
t.removeQuestion(1);       // guard used to require removed.id, leaving stale refs

check(t.questions.length === 2, 'question not removed');
const stale = t.questions.some(q => (q.condition_groups || []).some(group =>
  group.some(rule => rule.depends_on_uid === q2Uid || rule.depends_on_question_id === 'q2')));
check(!stale, 'conditions still reference the deleted id-less question');

t.initializeConditionUids();
check(t.collectInvalidConditionRefs().length === 0, 'no dangling refs after cleanup');
await t.saveForm();
check(fetchCalls.length === 1, 'save should proceed after cleanup');
const saved = fetchCalls[0].body.questions.find(q => q.id === 'q3').condition_groups;
check(JSON.stringify(saved) === JSON.stringify([[
  { depends_on_question_id: 'q1', operator: 'equals', expected_value: ['A'] },
]]), 'remaining group should keep only q1: ' + JSON.stringify(saved));
""")


def test_missing_parent_blocks_save_instead_of_silent_drop():
    _run_editor_js(r"""
t.questions[2].condition_groups[0][0].depends_on_uid = 'ghost_uid';
const builder = t.renderConditionalBuilderHtml(t.questions[2], 2);
check(builder.includes('-- Câu hỏi đã bị xóa (chọn lại) --'), 'missing parent not surfaced');

await t.saveForm();
check(fetchCalls.length === 0, 'dangling rule must not be silently dropped and saved');
check(alerts.length === 1 && alerts[0].includes('không còn tồn tại'), 'expected clear error: ' + alerts.join(' | '));
""")
