"""Admin Google Drive export configuration and manual execution."""
from __future__ import annotations

import html
import json
import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.drive_exports import (
    DEFAULT_FIELDS,
    EXPORT_FIELDS,
    EXPORT_STATUSES,
    VEHICLE_TYPES,
    DriveExportError,
    delete_client,
    get_client,
    get_settings,
    list_clients,
    run_drive_exports,
    save_client,
    save_settings,
)
from core.state import FORM_REGISTRY, get_db_pool, verify_admin_auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Drive Exports"])


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _checked(condition: bool) -> str:
    return " checked" if condition else ""


def _pool():
    pool = get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Drive export database unavailable; apply migration 009.")
    return pool


def _checkbox_group(name: str, options: list[tuple[str, str]], selected: list[str]) -> str:
    chosen = set(selected or [])
    return "".join(
        f'<label class="check"><input type="checkbox" name="{name}" value="{_esc(value)}"'
        f'{_checked(value in chosen)}> {_esc(label)}</label>'
        for value, label in options
    )


def _client_form(client: dict[str, Any] | None = None) -> str:
    raw = dict(client or {})
    slug = str(raw.get("slug") or "")
    kind = str(raw.get("kind") or "client")
    selected_forms = [str(v) for v in (raw.get("form_ids") or [])]
    selected_fields = [str(v) for v in (raw.get("fields") or DEFAULT_FIELDS)]
    selected_statuses = [str(v) for v in (raw.get("statuses") or ["APPROVED"])]
    selected_vehicles = [str(v) for v in (raw.get("vehicle_types") or [])]
    forms = sorted(set(str(fid) for fid in FORM_REGISTRY.keys()) | set(selected_forms))
    action = f"/admin/exports/clients/{slug}" if slug else "/admin/exports/clients"
    delete_form = ""
    if slug:
        delete_form = (
            f'<form method="post" action="/admin/exports/clients/{slug}/delete" '
            f'onsubmit="return confirm(\'Delete {slug}? Drive folders are kept.\')">'
            '<button class="danger" type="submit">Delete configuration</button></form>'
        )
    return f"""
<article class="card">
  <form method="post" action="{action}">
    <div class="grid">
      <label>Slug<input name="slug" value="{_esc(slug)}" pattern="[a-z0-9][a-z0-9_-]{{0,63}}" maxlength="64" {'readonly' if slug else 'required'}></label>
      <label>Display name<input name="display_name" value="{_esc(raw.get('display_name', ''))}" maxlength="100" required></label>
      <label>Kind<select name="kind">
        <option value="client"{' selected' if kind == 'client' else ''}>Client export</option>
        <option value="internal"{' selected' if kind == 'internal' else ''}>Internal export</option>
      </select></label>
      <label>Internal category<input name="category" value="{_esc(raw.get('category', ''))}" maxlength="100" placeholder="Required for internal"></label>
      <label>Drive folder name<input name="folder_name" value="{_esc(raw.get('folder_name', ''))}" maxlength="100" placeholder="Defaults to display name"></label>
      <label>Enabled<select name="enabled"><option value="on"{' selected' if bool(raw.get('enabled', True)) else ''}>Yes</option><option value="off"{' selected' if not bool(raw.get('enabled', True)) else ''}>No</option></select></label>
      <label>From date<input type="date" name="date_from" value="{_esc(raw.get('date_from', ''))}"></label>
      <label>To date<input type="date" name="date_to" value="{_esc(raw.get('date_to', ''))}"></label>
    </div>
    <div class="grid">
      <fieldset><legend>Forms</legend><p class="hint">No selection means every form present in the filtered submissions.</p>{_checkbox_group('form_ids', [(fid, fid) for fid in forms], selected_forms)}</fieldset>
      <fieldset><legend>Fields</legend>{_checkbox_group('fields', list(EXPORT_FIELDS), selected_fields)}</fieldset>
      <fieldset><legend>Statuses</legend>{_checkbox_group('statuses', [(status, status) for status in EXPORT_STATUSES], selected_statuses)}</fieldset>
      <fieldset><legend>Vehicle kinds</legend><p class="hint">No selection means all vehicles.</p>{_checkbox_group('vehicle_types', [(kind_name, kind_name) for kind_name in VEHICLE_TYPES], selected_vehicles)}</fieldset>
    </div>
    <label class="check"><input type="checkbox" name="include_images" value="on"{_checked(bool(raw.get('include_images', True)))}> Include submission images as shortcuts to the master image tree</label>
    <div class="actions"><button type="submit">{'Save changes' if slug else 'Create export'}</button></div>
  </form>
  {delete_form}
</article>"""


def _render(
    settings: dict[str, Any], clients: list[dict[str, Any]], *, message: str = "", error: str = ""
) -> str:
    report = settings.get("last_report")
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except ValueError:
            report = None
    report_text = json.dumps(report, ensure_ascii=False, indent=2) if report else "No export has run yet."
    cards = "".join(_client_form(client) for client in clients)
    flash = ""
    if message:
        flash = f'<div class="flash ok">{_esc(message)}</div>'
    if error:
        flash = f'<div class="flash bad">{_esc(error)}</div>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Drive Exports</title>
<style>
:root {{ color-scheme:dark; --bg:#0b1220; --panel:#131d30; --line:#2a3952; --fg:#e5edf7; --muted:#9aabc1; --accent:#38bdf8; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--fg); font:15px system-ui,sans-serif }} main {{ max-width:1200px; margin:auto; padding:24px }}
h1 {{ font-size:1.4rem }} h2 {{ font-size:1rem }} .hint,.muted {{ color:var(--muted); font-size:.82rem }} .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin:12px 0 }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px }} fieldset {{ border:1px solid var(--line); border-radius:8px; padding:10px }} legend {{ color:var(--accent); font-weight:700 }}
label {{ display:block; margin:8px 0; color:var(--muted); font-size:.85rem }} label.check {{ color:var(--fg); margin:4px 0 }} input,select,textarea {{ display:block; width:100%; margin-top:4px; padding:9px; background:#09111e; color:var(--fg); border:1px solid var(--line); border-radius:6px; font: .9rem ui-monospace,monospace }}
label.check input {{ display:inline; width:auto; margin-right:6px }} button {{ border:0; border-radius:6px; background:#0369a1; color:white; padding:8px 12px; cursor:pointer }} button.danger {{ background:#991b1b }} .actions {{ display:flex; gap:8px; margin-top:10px }} pre {{ overflow:auto; max-height:320px; background:#09111e; padding:10px; border:1px solid var(--line); border-radius:8px }} .flash {{ padding:10px; border:1px solid var(--line); border-radius:8px; margin:10px 0 }} .flash.ok {{ color:#86efac }} .flash.bad {{ color:#fca5a5 }} .row {{ display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap }}
</style></head><body><main>
<h1>Google Drive Exports</h1>
<p class="muted">Master layout: <code>&lt;master&gt;/Client Exports/&lt;client&gt;/&lt;form&gt;</code> and <code>&lt;master&gt;/Internal Exports/&lt;category&gt;/&lt;form&gt;</code>. Every form gets an output Google Sheet and an <code>Images</code> folder. Images are uploaded once under <code>Images Master</code>; client folders contain shortcuts, not copies.</p>
{flash}
<section class="card"><h2>Folder names</h2>
<form method="post" action="/admin/exports/settings"><div class="grid">
<label>Master export folder<input name="master_folder_name" value="{_esc(settings.get('master_folder_name', 'Master Export'))}" required></label>
<label>Client exports folder<input name="client_folder_name" value="{_esc(settings.get('client_folder_name', 'Client Exports'))}" required></label>
<label>Internal exports folder<input name="internal_folder_name" value="{_esc(settings.get('internal_folder_name', 'Internal Exports'))}" required></label>
<label>Images master folder<input name="images_folder_name" value="{_esc(settings.get('images_folder_name', 'Images Master'))}" required></label>
</div><div class="actions"><button type="submit">Save folder names</button></div></form></section>
<section class="card"><div class="row"><div><h2>Run export</h2><p class="hint">Runs enabled configurations now. A database advisory lock prevents overlapping runs.</p></div>
<form method="post" action="/admin/exports/run"><button type="submit">Run Google Drive export</button></form></div>
<h3>Last report</h3><pre>{_esc(report_text)}</pre></section>
<section><h2>Client and internal configurations</h2>{cards or '<p class="muted">No export configurations yet.</p>'}</section>
<section class="card"><h2>Create configuration</h2>{_client_form()}</section>
</main></body></html>"""


def _client_payload(form) -> dict[str, Any]:
    return {
        "slug": str(form.get("slug") or "").strip(),
        "display_name": str(form.get("display_name") or "").strip(),
        "kind": str(form.get("kind") or "client").strip(),
        "category": str(form.get("category") or "").strip(),
        "folder_name": str(form.get("folder_name") or "").strip(),
        "enabled": str(form.get("enabled") or "").lower() in {"on", "true", "1", "yes"},
        "date_from": str(form.get("date_from") or "").strip() or None,
        "date_to": str(form.get("date_to") or "").strip() or None,
        "form_ids": form.getlist("form_ids"),
        "fields": form.getlist("fields"),
        "statuses": form.getlist("statuses"),
        "vehicle_types": form.getlist("vehicle_types"),
        "include_images": str(form.get("include_images") or "").lower() in {"on", "true", "1", "yes"},
    }


@router.get("/admin/exports", response_class=HTMLResponse)
async def exports_page(
    message: str = "", error: str = "", _admin: str = Depends(verify_admin_auth),
):
    pool = _pool()
    try:
        settings = await get_settings(pool)
        clients = await list_clients(pool)
    except Exception as exc:
        logger.warning("drive export page query failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Drive export database unavailable.") from exc
    return HTMLResponse(_render(settings, clients, message=message, error=error))


@router.get("/api/v1/admin/exports")
async def exports_api(_admin: str = Depends(verify_admin_auth)):
    pool = _pool()
    return {"settings": await get_settings(pool), "clients": await list_clients(pool)}


@router.post("/admin/exports/settings")
async def save_export_settings(request: Request, admin_user: str = Depends(verify_admin_auth)):
    form = await request.form()
    try:
        await save_settings(
            _pool(), admin_user,
            master_folder_name=str(form.get("master_folder_name") or ""),
            client_folder_name=str(form.get("client_folder_name") or ""),
            internal_folder_name=str(form.get("internal_folder_name") or ""),
            images_folder_name=str(form.get("images_folder_name") or ""),
        )
    except (DriveExportError, ValueError) as exc:
        return RedirectResponse(f"/admin/exports?error={quote(str(exc), safe='')}", status_code=303)
    return RedirectResponse("/admin/exports?message=Folder+names+saved", status_code=303)


@router.post("/admin/exports/clients")
async def create_export_client(request: Request, admin_user: str = Depends(verify_admin_auth)):
    payload = _client_payload(await request.form())
    pool = _pool()
    if await get_client(pool, payload["slug"]):
        return RedirectResponse("/admin/exports?error=That+slug+already+exists", status_code=303)
    try:
        await save_client(pool, payload, admin_user)
    except (DriveExportError, ValueError) as exc:
        return RedirectResponse(f"/admin/exports?error={quote(str(exc), safe='')}", status_code=303)
    return RedirectResponse("/admin/exports?message=Export+configuration+created", status_code=303)


@router.post("/admin/exports/clients/{slug}")
async def update_export_client(slug: str, request: Request, admin_user: str = Depends(verify_admin_auth)):
    payload = _client_payload(await request.form())
    payload["slug"] = slug
    try:
        await save_client(_pool(), payload, admin_user)
    except (DriveExportError, ValueError) as exc:
        return RedirectResponse(f"/admin/exports?error={quote(str(exc), safe='')}", status_code=303)
    return RedirectResponse("/admin/exports?message=Export+configuration+saved", status_code=303)


@router.post("/admin/exports/clients/{slug}/delete")
async def remove_export_client(slug: str, admin_user: str = Depends(verify_admin_auth)):
    if not await delete_client(_pool(), slug, admin_user):
        return RedirectResponse("/admin/exports?error=Configuration+not+found", status_code=303)
    return RedirectResponse("/admin/exports?message=Export+configuration+deleted", status_code=303)


@router.post("/admin/exports/run")
async def run_exports(admin_user: str = Depends(verify_admin_auth)):
    pool = _pool()
    try:
        report = await run_drive_exports(pool)
    except Exception as exc:
        logger.warning("manual drive export failed: %s", type(exc).__name__)
        return RedirectResponse("/admin/exports?error=Export+run+failed", status_code=303)
    status = report.get("status", "unknown")
    return RedirectResponse(f"/admin/exports?message={quote('Export finished: ' + status, safe='')}", status_code=303)
