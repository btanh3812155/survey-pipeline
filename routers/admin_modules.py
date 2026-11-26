"""Authenticated administration for database-backed image and logic modules."""
from __future__ import annotations

import html
import json
import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.admin_modules import (
    ModuleDefinitionError,
    delete_module,
    execute_logic_module,
    list_modules,
    save_module,
    set_module_enabled,
)
from core.state import get_db_pool, verify_admin_auth
from services.logic_modules import SOURCES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin Modules"])


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _page(modules: list[dict[str, Any]], *, message: str = "", error: str = "") -> str:
    cards = []
    for module in modules:
        module_id = str(module["module_id"])
        kind = str(module["module_type"])
        definition = module["definition"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        enabled = bool(module["enabled"])
        card = f"""
<details class="module" data-search="{_esc(module_id + ' ' + module['display_name'] + ' ' + kind)}">
  <summary>
    <span class="title"><strong>{_esc(module['display_name'])}</strong> <code>{_esc(module_id)}</code></span>
    <span class="meta"><span class="badge">{_esc(kind)}</span><span class="badge">v{_esc(module['module_version'])}</span>
    <span class="badge {'enabled' if enabled else 'disabled'}">{'đang bật' if enabled else 'đang tắt'}</span></span>
  </summary>
  <form method="post" action="/admin/modules/{_esc(module_id)}">
    <div class="grid"><label>Tên<input name="display_name" value="{_esc(module['display_name'])}" maxlength="100" required></label>
      <label>Phiên bản module<input name="module_version" value="{_esc(module['module_version'])}" maxlength="32" required></label></div>
    <label>JSON định nghĩa<textarea name="definition" rows="14" spellcheck="false" required>{_esc(_json_text(definition))}</textarea></label>
    {'<p class="hint">Python không được nạp hoặc thực thi từ registry. Implementation này cố định trong mã ứng dụng.</p><pre>' + _esc(SOURCES.get(definition.get('implementation'), 'Implementation không xác định')) + '</pre>' if kind == 'logic' else ''}
    {'<p class="hint">Module check engine. Implementation nằm trong allowlist của mã ứng dụng; tham số và điều kiện là JSON khai báo.</p>' if kind == 'engine' else ''}
    <button type="submit">Lưu định nghĩa</button>
  </form>
  <div class="actions">
    <form method="post" action="/admin/modules/{_esc(module_id)}/enabled"><input type="hidden" name="enabled" value="{'false' if enabled else 'true'}"><button type="submit">{'Tắt' if enabled else 'Bật'}</button></form>
    <form method="post" action="/admin/modules/{_esc(module_id)}/delete" onsubmit="return confirm('Xóa {_esc(module_id)}?')"><button class="danger" type="submit">Xóa</button></form>
  </div>
  {f'<form method="post" action="/admin/modules/{_esc(module_id)}/run"><label>Dữ liệu thử (JSON)<textarea name="inputs" rows="3">{{"target": 7, "total": 10}}</textarea></label><button type="submit">Chạy an toàn</button></form>' if kind == 'logic' else ''}
</details>"""
        cards.append(card)
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Module Logic, Object &amp; Engine</title>
<style>
:root {{ color-scheme:dark; --bg:#0b1220; --panel:#131d30; --line:#2a3952; --fg:#e5edf7; --muted:#9aabc1; --accent:#38bdf8; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--fg); font:15px system-ui,sans-serif }} main {{ max-width:1100px; margin:auto; padding:24px }}
h1 {{ font-size:1.4rem }} h2 {{ font-size:1rem }} .hint,.muted {{ color:var(--muted) }} .flash {{ padding:10px; margin:12px 0; border:1px solid var(--line); border-radius:8px }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px }} .module {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin:12px 0 }}
.module > summary {{ display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; cursor:pointer; list-style:none; }}
.module > summary::-webkit-details-marker {{ display:none; }}
.module > summary::after {{ content:'▾'; color:var(--muted); }}
.module[open] > summary::after {{ transform:rotate(180deg); }}
.module > summary .meta {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; }}
.module > summary + form {{ margin-top:12px; }}
.actions {{ display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }} .badge {{ border:1px solid var(--line); border-radius:20px; padding:2px 8px; color:var(--muted); font-size:.75rem }} .enabled {{ color:#86efac }} .disabled {{ color:#fca5a5 }}
label {{ display:block; margin:10px 0; color:var(--muted); font-size:.85rem }} input,textarea,select {{ display:block; width:100%; margin-top:4px; padding:9px; background:#09111e; color:var(--fg); border:1px solid var(--line); border-radius:6px; font: .9rem ui-monospace,monospace }}
textarea {{ resize:vertical }} button {{ border:0; border-radius:6px; background:#0369a1; color:white; padding:8px 12px; cursor:pointer }} button.danger {{ background:#991b1b }} pre {{ overflow:auto; background:#09111e; padding:10px }} .actions {{ justify-content:flex-start; margin:10px 0 }} .actions form {{ display:inline }}
</style></head><body><main><h1>Module Logic, Object &amp; Engine</h1><p class="muted">Registry lưu trong cơ sở dữ liệu. Object định nghĩa cách trích xuất ảnh. Check engine là module engine trong cùng registry. Logic chỉ chạy implementation đã biên dịch trong ứng dụng; JSON lưu trữ không bao giờ thực thi mã.</p>
{f'<div class="flash">{_esc(message or error)}</div>' if message or error else ''}
<details class="module"><summary><strong>Tạo module</strong></summary><form method="post" action="/admin/modules" style="margin-top:12px;">
<div class="grid"><label>ID module<input name="module_id" pattern="[A-Za-z0-9_-]{1,64}" required></label><label>Loại<select name="module_type" id="moduleType" data-searchable-select onchange="setModuleTemplate(this.value)"><option value="object">Object ảnh</option><option value="logic">Logic</option><option value="engine">Check engine</option></select></label><label>Tên<input name="display_name" maxlength="100" required></label><label>Phiên bản module<input name="module_version" maxlength="32" value="1" required></label></div>
<label>JSON định nghĩa<textarea id="moduleDefinition" name="definition" rows="10" required>{{"version":1,"label":"Object ảnh mới","prompt":"Inspect {{{{objects}}}}. Expected fields: {{{{fields}}}}. Configuration: {{{{config}}}}.","objects":[],"expected_fields":[],"customizable":[],"engines":[],"extractor":"generic","json_output_instruction":false}}</textarea></label>
<p class="hint">Định nghĩa logic phải tham chiếu implementation trong allowlist, ví dụ <code>get_percentage</code>; định nghĩa engine phải tham chiếu implementation như <code>required_fields</code>. Muốn thêm Python tùy ý phải thay đổi mã ứng dụng và được review.</p><button type="submit">Tạo module</button></form></details>
<div class="grid"><label>Tìm module<input id="search" type="search" placeholder="Tên, id hoặc loại" oninput="filterModules()"></label></div>
<section><h2>Module đã đăng ký ({len(modules)})</h2><div id="modulesList" data-tb-paginate="details.module[data-search]" data-page-size="10">{''.join(cards) or '<p class="muted">Chưa có module nào.</p>'}</div></section>
</main><script>
function filterModules() {{
  const query = document.getElementById('search').value.trim().toLowerCase();
  const list = document.getElementById('modulesList');
  for (const item of document.querySelectorAll('.module[data-search]')) {{
    const ok = item.dataset.search.toLowerCase().includes(query);
    item.dataset.filterHidden = ok ? '0' : '1';
    if (!list?._tbPaginateRender) item.hidden = !ok;
  }}
  if (list?._tbPaginateRender) list._tbPaginateRender();
}}
const MODULE_TEMPLATES = {{
  object: {{"version":1,"label":"Object ảnh mới","prompt":"Inspect {{{{objects}}}}. Expected fields: {{{{fields}}}}. Configuration: {{{{config}}}}.","objects":[],"expected_fields":[],"customizable":[],"engines":[],"extractor":"generic","json_output_instruction":false}},
  logic: {{"id":"new-logic","name":"new-logic","version":"1","description":"","implementation":"get_percentage","inputs":[{{"name":"target","type":"integer","required":true}},{{"name":"total","type":"integer","required":true}}]}},
  engine: {{"version":1,"id":"new-engine","label":"Check engine mới","description":"","implementation":"required_fields","params_schema":[{{"key":"fields","label":"Trường","type":"array","required":true}}],"conditions_schema":[]}},
}};
function setModuleTemplate(kind) {{
  const el = document.getElementById('moduleDefinition');
  if (el) el.value = JSON.stringify(MODULE_TEMPLATES[kind] || MODULE_TEMPLATES.object, null, 2);
}}
</script></body></html>"""


async def _modules() -> list[dict[str, Any]]:
    pool = get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Registry module không khả dụng; hãy áp dụng migration 008.")
    try:
        return await list_modules(pool)
    except Exception as exc:
        logger.warning("module registry query failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Registry module không khả dụng; hãy áp dụng migration 008.") from exc


def _pool_or_503():
    pool = get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Registry module không khả dụng; hãy áp dụng migration 008.")
    return pool


@router.get("/admin/modules", response_class=HTMLResponse)
async def modules_page(_admin: str = Depends(verify_admin_auth), message: str = "", error: str = ""):
    return HTMLResponse(_page(await _modules(), message=message, error=error))


@router.get("/api/v1/admin/modules")
async def modules_api(_admin: str = Depends(verify_admin_auth)):
    modules = await _modules()
    for module in modules:
        definition = module["definition"]
        if isinstance(definition, str):
            module["definition"] = json.loads(definition)
    return {"modules": modules}


@router.post("/admin/modules")
async def create_module(request: Request, admin_user: str = Depends(verify_admin_auth)):
    form = await request.form()
    module_id = str(form.get("module_id") or "").strip()
    module_type = str(form.get("module_type") or "object")
    try:
        definition = json.loads(str(form.get("definition") or "{}"))
        await save_module(
            _pool_or_503(), module_id, module_type, str(form.get("display_name") or ""),
            str(form.get("module_version") or "1"), definition, True, admin_user,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("module create failed: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Không thể tạo module.") from exc
    return RedirectResponse("/admin/modules?message=" + quote("Đã tạo module"), status_code=303)


@router.post("/admin/modules/{module_id}")
async def update_module(module_id: str, request: Request, admin_user: str = Depends(verify_admin_auth)):
    form = await request.form()
    current = next((item for item in await _modules() if item["module_id"] == module_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy module.")
    try:
        definition = json.loads(str(form.get("definition") or "{}"))
        await save_module(
            _pool_or_503(), module_id, str(current["module_type"]),
            str(form.get("display_name") or ""), str(form.get("module_version") or ""),
            definition, bool(current["enabled"]), admin_user,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("module save failed: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Không thể lưu module.") from exc
    return RedirectResponse("/admin/modules?message=" + quote("Đã lưu module"), status_code=303)


@router.post("/admin/modules/{module_id}/enabled")
async def toggle_module(module_id: str, request: Request, admin_user: str = Depends(verify_admin_auth)):
    form = await request.form()
    enabled = str(form.get("enabled") or "").lower() in {"1", "true", "yes", "on"}
    try:
        changed = await set_module_enabled(_pool_or_503(), module_id, enabled, admin_user)
    except ModuleDefinitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="Không tìm thấy module.")
    return RedirectResponse("/admin/modules?message=" + quote("Đã cập nhật module"), status_code=303)


@router.post("/admin/modules/{module_id}/delete")
async def remove_module(module_id: str, admin_user: str = Depends(verify_admin_auth)):
    try:
        deleted = await delete_module(_pool_or_503(), module_id, admin_user)
    except ModuleDefinitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Không tìm thấy module.")
    return RedirectResponse("/admin/modules?message=" + quote("Đã xóa module"), status_code=303)


@router.post("/admin/modules/{module_id}/run")
async def run_logic_module(module_id: str, request: Request, _admin: str = Depends(verify_admin_auth)):
    form = await request.form()
    try:
        inputs = json.loads(str(form.get("inputs") or "{}"))
        result = await execute_logic_module(_pool_or_503(), module_id, inputs)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("logic module execution rejected: %s", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Không thể chạy module logic.") from exc
    return HTMLResponse(_page(await _modules(), message=f"{module_id}: {_json_text(result)}"))
