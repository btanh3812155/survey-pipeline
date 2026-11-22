# -*- coding: utf-8 -*-
"""
Admin management for file-defined image object types and check engines.
"""

import html
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.state import get_db_pool, verify_admin_auth
from core.admin_modules import delete_module, save_module
from services.image_check_engines import ENGINE_IMPLEMENTATIONS
from services.image_objects import (
    ImageObjectDefinitionError,
    delete_object,
    get_definition_errors,
    get_engine,
    list_engines,
    list_objects,
    load_registry,
    reload_registry,
    save_engine,
    save_object,
)

logger = logging.getLogger("routers.admin_image_objects")
router = APIRouter(tags=["Image Objects"])

_EXTRACTORS = ("generic", "income_statement", "profile", "vehicle", "none")


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _json_text(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, indent=2)


def _parse_json_field(value: Any, field: str, default: Any):
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field}: JSON không hợp lệ ({exc.msg})")


def _object_form(obj: Any) -> str:
    raw = obj.model_dump(mode="json") if obj is not None else {}
    object_id = _esc(raw.get("id", ""))
    return f"""
    <details class="card" {'open' if obj is not None else ''}>
      <summary><span class="mono">{object_id or '＋ Tạo object mới'}</span> <span class="muted">{_esc(raw.get('label', ''))}</span></summary>
      <div class="src">Module registry definition: {object_id or '&lt;id&gt;'} (database-authoritative when migration 008 is active)</div>
      <form method="post" action="/admin/image-objects/{object_id}">
        <div class="grid">
          <label>ID<input name="id" value="{object_id}" {'disabled' if obj is not None else 'required'}></label>
          <label>Label<input name="label" value="{_esc(raw.get('label', ''))}" required></label>
          <label>Extractor<select name="extractor">{''.join(f'<option value="{_esc(v)}" {"selected" if raw.get("extractor", "generic") == v else ""}>{_esc(v)}</option>' for v in _EXTRACTORS)}</select></label>
          <label>Classify role<input name="classify_role" value="{_esc(raw.get('classify_role', ''))}"></label>
        </div>
        <label>Description<textarea name="description" rows="2">{_esc(raw.get('description', ''))}</textarea></label>
        <label>Prompt<textarea name="prompt" rows="7">{_esc(raw.get('prompt', ''))}</textarea></label>
        <div class="grid">
          <label>Objects (mỗi dòng một mục)<textarea name="objects" rows="4">{_esc(chr(10).join(raw.get('objects', [])))}</textarea></label>
          <label>Expected fields (JSON)<textarea name="expected_fields" rows="10">{_esc(_json_text(raw.get('expected_fields', [])))}</textarea></label>
        </div>
        <div class="grid">
          <label>Customizable (JSON)<textarea name="customizable" rows="10">{_esc(_json_text(raw.get('customizable', [])))}</textarea></label>
          <label>Engines (JSON)<textarea name="engines" rows="10">{_esc(_json_text(raw.get('engines', [])))}</textarea></label>
        </div>
        <div class="actions">
          <button type="submit">Lưu object</button>
          {f'<button type="button" class="danger" onclick="deleteObject(\'{object_id}\')">Xóa object</button>' if obj is not None else ''}
        </div>
      </form>
    </details>
    """


def _engine_card(engine: Any) -> str:
    raw = engine.model_dump(mode="json")
    engine_id = _esc(raw["id"])
    options = "".join(
        f'<option value="{_esc(name)}" {"selected" if name == raw["implementation"] else ""}>{_esc(name)}</option>'
        for name in sorted(ENGINE_IMPLEMENTATIONS)
    )
    return f"""
    <details class="card">
      <summary><span class="mono">{engine_id}</span> <span class="muted">{_esc(raw.get("label", ""))}</span></summary>
      <div class="src">File: data/check_engines/{engine_id}.json</div>
      <form method="post" action="/admin/image-objects/engines/{engine_id}">
        <div class="grid">
          <label>ID<input value="{engine_id}" disabled></label>
          <label>Label<input name="label" value="{_esc(raw.get('label', ''))}" required></label>
          <label>Implementation<select name="implementation">{options}</select></label>
        </div>
        <label>Description<textarea name="description" rows="2">{_esc(raw.get('description', ''))}</textarea></label>
        <div class="grid">
          <label>Params schema (JSON)<textarea name="params_schema" rows="9">{_esc(_json_text(raw.get('params_schema', [])))}</textarea></label>
          <label>Conditions schema (JSON)<textarea name="conditions_schema" rows="9">{_esc(_json_text(raw.get('conditions_schema', [])))}</textarea></label>
        </div>
        <div class="actions"><button type="submit">Lưu engine</button></div>
      </form>
    </details>
    """


def render_image_objects_html(message: str = "", error: str = "", selected: Optional[str] = None) -> str:
    registry = load_registry()
    objects = list(registry["objects"].values())
    engines = list(registry["engines"].values())
    errors = get_definition_errors()
    paths = registry["paths"]

    errors_html = ""
    if errors:
        rows = "".join(
            f"<tr><td class='mono'>{_esc(item.get('file'))}</td><td>{_esc(item.get('scope'))}</td>"
            f"<td class='mono'>{_esc(item.get('path'))}</td><td class='err'>{_esc(item.get('error'))}</td></tr>"
            for item in errors
        )
        errors_html = f"""
        <section class="panel bad">
          <h2>Lỗi định nghĩa ({len(errors)})</h2>
          <table><thead><tr><th>File</th><th>Loại</th><th>Đường dẫn</th><th>Lỗi</th></tr></thead><tbody>{rows}</tbody></table>
        </section>"""

    object_cards = "".join(
        _object_form(obj) for obj in sorted(objects, key=lambda o: o.id)
    )
    engine_cards = "".join(
        _engine_card(engine) for engine in sorted(engines, key=lambda e: e.id)
    )
    banner = ""
    if message:
        banner += f"<div class='flash ok'>{_esc(message)}</div>"
    if error:
        banner += f"<div class='flash bad'>{_esc(error)}</div>"

    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Image Objects | Admin</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; font-family: 'Be Vietnam Pro', system-ui, sans-serif; background:#0f172a; color:#e2e8f0; }}
  header {{ display:flex; align-items:center; justify-content:space-between; padding:18px 24px; background:#1e293b; border-bottom:1px solid #334155; }}
  h1 {{ font-size:1.05rem; margin:0; }}
  main {{ padding:24px; max-width:1400px; margin:0 auto; }}
  .panel, .card {{ background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; margin-bottom:14px; }}
  .panel.bad, .flash.bad {{ border-color:#7f1d1d; background:#2a1215; }}
  .flash {{ padding:10px 14px; border-radius:8px; margin-bottom:14px; }}
  .flash.ok {{ border:1px solid #14532d; background:#0f2417; }}
  details > summary {{ cursor:pointer; font-weight:600; padding:2px 0; }}
  .mono {{ font-family: ui-monospace, monospace; color:#67e8f9; }}
  .muted {{ color:#94a3b8; font-weight:400; margin-left:8px; }}
  .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:10px; }}
  label {{ display:block; font-size:0.78rem; color:#cbd5e1; margin:8px 0 2px; }}
  input, textarea, select {{ width:100%; box-sizing:border-box; background:#0f172a; color:#e2e8f0; border:1px solid #475569; border-radius:6px; padding:7px; font-size:0.8rem; font-family: ui-monospace, monospace; }}
  textarea {{ resize:vertical; }}
  button {{ background:#0891b2; color:#fff; border:0; border-radius:6px; padding:8px 14px; font-weight:600; cursor:pointer; }}
  button.danger {{ background:#b91c1c; }}
  .actions {{ display:flex; gap:8px; margin-top:12px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.78rem; }}
  th, td {{ text-align:left; padding:6px; border-bottom:1px solid #334155; vertical-align:top; }}
  .err {{ color:#fca5a5; }}
  nav a {{ color:#67e8f9; margin-right:14px; text-decoration:none; }}
  h2 {{ font-size:0.9rem; margin:0 0 10px; }}
  .src {{ font-size:0.72rem; color:#64748b; margin-bottom:10px; }}
</style></head>
<body>
<header><h1>Image Object Types &amp; Check Engines</h1></header>
<main>
  {banner}
  {errors_html}
  <section class="panel">
    <h2>Tạo object mới</h2>
    <div class="src">Objects: {_esc(paths['objects'])} · Engines: {_esc(paths['engines'])}</div>
    <form method="post" action="/admin/image-objects/new_object">
      <div class="grid">
        <label>ID (chữ thường, a-z0-9_)<input name="id" required pattern="[a-z0-9_]+"></label>
        <label>Label<input name="label" required></label>
        <label>Extractor<select name="extractor">{''.join(f'<option value="{_esc(v)}">{_esc(v)}</option>' for v in _EXTRACTORS)}</select></label>
        <label>Classify role<input name="classify_role" placeholder="app_income"></label>
      </div>
      <div class="actions"><button type="submit">Tạo object</button></div>
    </form>
  </section>
  <section class="panel">
    <h2>Object types ({len(objects)})</h2>
    {object_cards or '<div class="muted">Chưa có object nào.</div>'}
  </section>
  <section class="panel">
    <h2>Check engines ({len(engines)})</h2>
    {engine_cards or '<div class="muted">Chưa có engine nào.</div>'}
  </section>
  <section class="panel">
    <h2>Engine inspector (built-in registry)</h2>
    <table><thead><tr><th>Implementation</th><th>Mô tả</th></tr></thead><tbody>
      {''.join(f"<tr><td class='mono'>{_esc(name)}</td><td>Built-in Python engine; JSON không thể chứa mã thực thi.</td></tr>" for name in sorted(ENGINE_IMPLEMENTATIONS))}
    </tbody></table>
  </section>
</main>
<script>
async function deleteObject(objectId) {{
  if (!confirm('Xóa object ' + objectId + '?')) return;
  const resp = await fetch('/admin/image-objects/' + encodeURIComponent(objectId), {{ method: 'DELETE' }});
  const body = await resp.json().catch(() => ({{}}));
  if (resp.ok) window.location.href = '/admin/image-objects?message=' + encodeURIComponent('Đã xóa ' + objectId);
  else alert(body.detail || 'Không xóa được object');
}}
</script>
</body></html>"""


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("content-type") or "").lower() or "application/json" in (
        request.headers.get("accept") or ""
    ).lower()


async def _payload(request: Request) -> Dict[str, Any]:
    if _wants_json(request) and "application/json" in (request.headers.get("content-type") or "").lower():
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        return data
    form = await request.form()
    return {key: form.get(key) for key in form.keys()}


def _object_payload(data: Dict[str, Any], object_id: str) -> Dict[str, Any]:
    payload = dict(data)
    payload.pop("id", None)
    payload["id"] = object_id
    payload["version"] = int(payload.get("version") or 1)
    objects = payload.get("objects", [])
    if isinstance(objects, str):
        objects = [line.strip() for line in objects.splitlines() if line.strip()]
    payload["objects"] = objects or []
    payload["expected_fields"] = _parse_json_field(payload.get("expected_fields"), "expected_fields", [])
    payload["customizable"] = _parse_json_field(payload.get("customizable"), "customizable", [])
    payload["engines"] = _parse_json_field(payload.get("engines"), "engines", [])
    return payload


@router.get("/admin/image-objects", response_class=HTMLResponse, include_in_schema=False)
async def admin_image_objects_page(
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    if request.query_params.get("reload") in ("1", "true"):
        reload_registry()
    return HTMLResponse(
        render_image_objects_html(
            message=request.query_params.get("message", ""),
            error=request.query_params.get("error", ""),
        )
    )


@router.get("/api/v1/image-objects")
async def api_image_objects(
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    if request.query_params.get("reload") in ("1", "true"):
        registry = reload_registry()
    else:
        registry = load_registry()
    return JSONResponse(
        {
            "objects": [registry["objects_raw"][key] for key in sorted(registry["objects_raw"])],
            "engines": [registry["engines_raw"][key] for key in sorted(registry["engines_raw"])],
        }
    )


@router.post("/admin/image-objects/new_object")
async def create_image_object(request: Request, admin_user: str = Depends(verify_admin_auth)):
    data = await _payload(request)
    object_id = str(data.get("id") or "").strip()
    if not object_id:
        raise HTTPException(status_code=400, detail="id is required")
    payload = {
        "version": 1,
        "label": data.get("label") or object_id,
        "description": "",
        "prompt": "",
        "objects": [],
        "expected_fields": [],
        "customizable": [],
        "engines": [],
        "extractor": data.get("extractor") or "generic",
        "classify_role": data.get("classify_role") or None,
    }
    try:
        save_object(object_id, payload)
        pool = get_db_pool()
        if pool:
            try:
                await save_module(pool, object_id, "object", str(payload["label"]), "1", payload, True, admin_user)
            except Exception as exc:
                logger.warning("[IMAGE OBJECTS] module registry mirror failed: %s", type(exc).__name__)
    except (ValueError, ImageObjectDefinitionError) as exc:
        if _wants_json(request):
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(
            url=f"/admin/image-objects?error={html.escape(str(exc), quote=True)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if _wants_json(request):
        return {"status": "ok", "object_id": object_id}
    return RedirectResponse(
        url=f"/admin/image-objects?message=Đã+tạo+{object_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/image-objects/{object_id}")
async def save_image_object(
    object_id: str,
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    data = await _payload(request)
    try:
        payload = _object_payload(data, object_id)
        save_object(object_id, payload)
        pool = get_db_pool()
        if pool:
            try:
                await save_module(
                    pool, object_id, "object", str(payload.get("label") or object_id),
                    str(payload.get("version") or "1"), payload, True, admin_user,
                )
            except Exception as exc:
                logger.warning("[IMAGE OBJECTS] module registry mirror failed: %s", type(exc).__name__)
    except (ValueError, ImageObjectDefinitionError) as exc:
        logger.warning("[IMAGE OBJECTS] save %s failed: %s", object_id, exc)
        if _wants_json(request):
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(
            url=f"/admin/image-objects?error={html.escape(str(exc), quote=True)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if _wants_json(request):
        return {"status": "ok", "object_id": object_id}
    return RedirectResponse(
        url=f"/admin/image-objects?message=Đã+lưu+{object_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.delete("/admin/image-objects/{object_id}")
async def delete_image_object(object_id: str, admin_user: str = Depends(verify_admin_auth)):
    try:
        deleted = delete_object(object_id)
        pool = get_db_pool()
        if pool:
            try:
                await delete_module(pool, object_id, admin_user)
            except Exception as exc:
                logger.warning("[IMAGE OBJECTS] module registry mirror failed: %s", type(exc).__name__)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"object '{object_id}' not found")
    return {"status": "ok", "object_id": object_id, "deleted": True}


@router.post("/admin/image-objects/engines/{engine_id}")
async def save_check_engine(
    engine_id: str,
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    data = await _payload(request)
    try:
        existing = get_engine(engine_id)
    except ImageObjectDefinitionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    implementation = str(data.get("implementation") or existing.implementation)
    if implementation not in ENGINE_IMPLEMENTATIONS:
        raise HTTPException(status_code=400, detail="unknown implementation")
    payload = {
        "version": int(data.get("version") or existing.version),
        "id": engine_id,
        "label": data.get("label") or existing.label,
        "description": data.get("description", existing.description),
        "implementation": implementation,
        "params_schema": _parse_json_field(data.get("params_schema"), "params_schema", existing.model_dump()["params_schema"]),
        "conditions_schema": _parse_json_field(data.get("conditions_schema"), "conditions_schema", existing.model_dump()["conditions_schema"]),
    }
    try:
        save_engine(engine_id, payload)
    except (ValueError, ImageObjectDefinitionError) as exc:
        logger.warning("[IMAGE ENGINES] save %s failed: %s", engine_id, exc)
        if _wants_json(request):
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(
            url=f"/admin/image-objects?error={html.escape(str(exc), quote=True)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if _wants_json(request):
        return {"status": "ok", "engine_id": engine_id}
    return RedirectResponse(
        url=f"/admin/image-objects?message=Đã+lưu+engine+{engine_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
