"""Admin Google Drive export configuration and manual execution."""
from __future__ import annotations

import html
import json
import logging
import re
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
    delete_client_form,
    effective_form_config,
    get_client,
    get_settings,
    list_clients,
    run_drive_exports,
    save_client,
    save_client_form,
    save_settings,
)
from core.state import FORM_REGISTRY, get_db_pool, verify_admin_auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Drive Exports"])
_FORM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

STYLE = """
:root { color-scheme:dark; --bg:#0b1220; --panel:#131d30; --line:#2a3952; --fg:#e5edf7; --muted:#9aabc1; --accent:#38bdf8; }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--fg); font:15px system-ui,sans-serif } main { max-width:1200px; margin:auto; padding:24px }
h1 { font-size:1.4rem } h2 { font-size:1rem } h3 { font-size:.92rem } .hint,.muted { color:var(--muted); font-size:.82rem } .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin:12px 0 }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px } fieldset { border:1px solid var(--line); border-radius:8px; padding:10px } legend { color:var(--accent); font-weight:700 }
label { display:block; margin:8px 0; color:var(--muted); font-size:.85rem } label.check { color:var(--fg); margin:4px 0 } input,select,textarea { display:block; width:100%; margin-top:4px; padding:9px; background:#09111e; color:var(--fg); border:1px solid var(--line); border-radius:6px; font:.9rem ui-monospace,monospace }
label.check input { display:inline; width:auto; margin-right:6px } button { border:0; border-radius:6px; background:#0369a1; color:white; padding:8px 12px; cursor:pointer } button.danger { background:#991b1b } .actions { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap } pre { overflow:auto; max-height:320px; background:#09111e; padding:10px; border:1px solid var(--line); border-radius:8px } .flash { padding:10px; border:1px solid var(--line); border-radius:8px; margin:10px 0 } .flash.ok { color:#86efac } .flash.bad { color:#fca5a5 } .row { display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap }
.crumbs { display:flex; gap:8px; flex-wrap:wrap; align-items:center; color:var(--muted); font-size:.9rem } .crumbs a { color:var(--accent); text-decoration:none } .crumbs a:hover { text-decoration:underline }
.tree { display:flex; flex-direction:column; gap:10px } .tree-group { border-left:2px solid var(--line); padding-left:12px } .tree-client { border:1px solid var(--line); border-radius:8px; background:#0b1424; padding:10px 12px }
.tree-folder { display:flex; align-items:center; gap:8px; flex-wrap:wrap; color:var(--fg); text-decoration:none; font-weight:700 } .tree-folder:hover { color:var(--accent) } .tree-children { margin:8px 0 0 22px; display:flex; flex-direction:column; gap:4px; border-left:1px dashed var(--line); padding-left:12px }
.tree-form { display:flex; align-items:center; gap:8px; flex-wrap:wrap; color:#cbd5e1; text-decoration:none; font-size:.86rem; padding:4px 0 } .tree-form:hover { color:var(--accent) } .badge { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; color:var(--muted); font-size:.72rem }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _checked(condition: bool) -> str:
    return " checked" if condition else ""


def _pool():
    pool = get_db_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Drive export database unavailable; apply migration 009.")
    return pool


async def _client_or_404(slug: str) -> dict[str, Any]:
    client = await get_client(_pool(), slug)
    if not client:
        raise HTTPException(status_code=404, detail="Export configuration not found")
    return client


def _checkbox_group(name: str, options: list[tuple[str, str]], selected: list[str]) -> str:
    chosen = set(selected or [])
    return "".join(
        f'<label class="check"><input type="checkbox" name="{name}" value="{_esc(value)}"'
        f'{_checked(value in chosen)}> {_esc(label)}</label>'
        for value, label in options
    )


def _available_forms(client: dict[str, Any] | None = None) -> list[str]:
    raw = dict(client or {})
    return sorted(
        set(str(fid) for fid in FORM_REGISTRY)
        | set(str(v) for v in (raw.get("form_ids") or []))
        | set(str(v) for v in (raw.get("form_settings") or {}))
    )


def _flash(message: str = "", error: str = "") -> str:
    if message:
        return f'<div class="flash ok">{_esc(message)}</div>'
    if error:
        return f'<div class="flash bad">{_esc(error)}</div>'
    return ""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"""


def _crumbs(items: list[tuple[str, str | None]]) -> str:
    parts = []
    for label, href in items:
        if href:
            parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
        else:
            parts.append(f"<span>{_esc(label)}</span>")
    return '<nav class="crumbs">' + " <span>/</span> ".join(parts) + "</nav>"


def _client_form(client: dict[str, Any] | None = None, *, create: bool = False) -> str:
    raw = dict(client or {})
    slug = str(raw.get("slug") or "")
    kind = str(raw.get("kind") or "client")
    selected_forms = [str(v) for v in (raw.get("form_ids") or [])]
    action = "/admin/exports/clients" if create else f"/admin/exports/clients/{_esc(slug)}"
    forms = _available_forms(raw)
    label = slug or "new export configuration"
    confirm = f"return confirm('Save changes to {_esc(label)}?')"
    return f"""
<article class="card">
  <form method="post" action="{action}" onsubmit="{confirm}">
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
    <fieldset><legend>Forms this client receives</legend>
      <p class="hint">No selection means every active form. Use the form rows after saving to edit each form's output.</p>
      {_checkbox_group('form_ids', [(fid, fid) for fid in forms], selected_forms)}
    </fieldset>
    <div class="actions"><button type="submit">{'Create export' if create else 'Save client and form selection'}</button></div>
  </form>
</article>"""


def _form_rows(client: dict[str, Any]) -> str:
    overrides = client.get("form_settings") or {}
    form_ids = sorted(set(str(v) for v in (client.get("form_ids") or [])) | set(overrides))
    if not form_ids:
        return '<div class="muted">All active forms are selected; add a per-form override from the tree.</div>'
    return "".join(
        f'<a class="tree-form" href="/admin/exports/{_esc(client["slug"])}/{_esc(form_id)}">'
        f'<span>📄 {_esc(form_id)}</span>'
        f'<span class="badge">{"custom output" if form_id in overrides else "client defaults"}</span></a>'
        for form_id in form_ids
    )


def _tree(clients: list[dict[str, Any]]) -> str:
    if not clients:
        return '<p class="muted">No export configurations yet. Create one below.</p>'
    groups = {"client": "Clients", "internal": "Internal categories"}
    sections = []
    for kind, title in groups.items():
        entries = [c for c in clients if str(c.get("kind")) == kind]
        if not entries:
            continue
        branches = []
        for client in entries:
            slug = str(client.get("slug") or "")
            branches.append(f"""
<div class="tree-client">
  <a class="tree-folder" href="/admin/exports/{_esc(slug)}">
    <span>📁 {_esc(client.get('display_name') or slug)}</span>
    <span class="badge">{_esc(slug)}</span>
    <span class="badge">{len(client.get('form_ids') or []) or 'all'} forms</span>
  </a>
  <div class="tree-children">{_form_rows(client)}</div>
</div>""")
        sections.append(f'<section class="tree-group"><h3>{_esc(title)}</h3>{"".join(branches)}</section>')
    return '<div class="tree">' + "".join(sections) + "</div>"


def _render_root(
    settings: dict[str, Any], clients: list[dict[str, Any]], *, message: str = "", error: str = ""
) -> str:
    report = settings.get("last_report")
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except ValueError:
            report = None
    report_text = json.dumps(report, ensure_ascii=False, indent=2) if report else "No export has run yet."
    body = f"""
{_crumbs([('Drive Exports', '/admin/exports')])}
<h1>Google Drive Exports</h1>
<p class="muted">Navigate the tree to a client folder, then to a form folder. The client folder controls which forms are received; the form folder controls fields, statuses, vehicles, dates, and images for that client only.</p>
{_flash(message, error)}
<section class="card"><h2>Directory tree</h2>{_tree(clients)}</section>
<section class="card"><h2>Folder names</h2>
<form method="post" action="/admin/exports/settings" onsubmit="return confirm('Save Drive folder names?')"><div class="grid">
<label>Master export folder<input name="master_folder_name" value="{_esc(settings.get('master_folder_name', 'Master Export'))}" required></label>
<label>Client exports folder<input name="client_folder_name" value="{_esc(settings.get('client_folder_name', 'Client Exports'))}" required></label>
<label>Internal exports folder<input name="internal_folder_name" value="{_esc(settings.get('internal_folder_name', 'Internal Exports'))}" required></label>
<label>Images master folder<input name="images_folder_name" value="{_esc(settings.get('images_folder_name', 'Images Master'))}" required></label>
</div><div class="actions"><button type="submit">Save folder names</button></div></form></section>
<section class="card"><div class="row"><div><h2>Run export</h2><p class="hint">Runs enabled configurations now. A database advisory lock prevents overlapping runs.</p></div>
<form method="post" action="/admin/exports/run" onsubmit="return confirm('Run Google Drive export now?')"><button type="submit">Run Google Drive export</button></form></div>
<h3>Last report</h3><pre>{_esc(report_text)}</pre></section>
<section class="card"><h2>Create configuration</h2>{_client_form(create=True)}</section>"""
    return _page("Google Drive Exports", body)


def _render_client(
    client: dict[str, Any], *, message: str = "", error: str = ""
) -> str:
    slug = str(client.get("slug") or "")
    body = f"""
{_crumbs([('Drive Exports', '/admin/exports'), (str(client.get('display_name') or slug), None)])}
<h1>📁 {_esc(client.get('display_name') or slug)}</h1>
<p class="muted">This folder controls which forms the {_esc(client.get('kind'))} receives. Open a form folder to edit its output for this client.</p>
{_flash(message, error)}
<section class="card"><h2>Forms this folder receives</h2>{_client_form(client)}</section>
<section class="card"><h2>Form output folders</h2><div class="tree-children" style="margin-left:0;border-left:0;padding-left:0;">{_form_rows(client)}</div></section>
<section class="card"><h2>Danger zone</h2>
<form method="post" action="/admin/exports/clients/{_esc(slug)}/delete" onsubmit="return confirm('Delete {_esc(slug)}? Drive folders are kept.')">
<button class="danger" type="submit">Delete configuration</button></form></section>"""
    return _page(f"Export {slug}", body)


def _render_form(
    client: dict[str, Any],
    form_id: str,
    *,
    message: str = "",
    error: str = "",
) -> str:
    slug = str(client.get("slug") or "")
    override = (client.get("form_settings") or {}).get(form_id) or {}
    effective = effective_form_config(client, form_id)
    selected_forms = set(str(v) for v in (client.get("form_ids") or []))
    warning = ""
    if selected_forms and form_id not in selected_forms:
        warning = f'<div class="flash bad">This form is not selected for the client yet. Saving this form adds it to the client.</div>'
    date_hint = (
        f"Blank means inherit client range "
        f"({_esc(client.get('date_from') or 'any')} to {_esc(client.get('date_to') or 'any')})."
    )
    body = f"""
{_crumbs([('Drive Exports', '/admin/exports'), (str(client.get('display_name') or slug), f'/admin/exports/{slug}'), (f'📄 {form_id}', None)])}
<h1>📄 {_esc(form_id)}</h1>
<p class="muted">Output settings for <b>{_esc(client.get('display_name') or slug)}</b>. These override the client defaults for this form only.</p>
{_flash(message, error)}{warning}
<section class="card">
<form method="post" action="/admin/exports/clients/{_esc(slug)}/forms/{_esc(form_id)}" onsubmit="return confirm('Save output settings for {_esc(slug)} / {_esc(form_id)}?')">
  <div class="grid">
    <fieldset><legend>Fields</legend><p class="hint">Unchecking everything falls back to the client field list.</p>{_checkbox_group('fields', list(EXPORT_FIELDS), list(effective.get('fields') or []))}</fieldset>
    <fieldset><legend>Statuses</legend>{_checkbox_group('statuses', [(status, status) for status in EXPORT_STATUSES], list(effective.get('statuses') or []))}</fieldset>
    <fieldset><legend>Vehicle kinds</legend><p class="hint">No selection means all vehicles.</p>{_checkbox_group('vehicle_types', [(kind, kind) for kind in VEHICLE_TYPES], list(effective.get('vehicle_types') or []))}</fieldset>
  </div>
  <div class="grid">
    <label>From date<input type="date" name="date_from" value="{_esc(override.get('date_from') or '')}"></label>
    <label>To date<input type="date" name="date_to" value="{_esc(override.get('date_to') or '')}"></label>
  </div>
  <p class="hint">{date_hint}</p>
  <label class="check"><input type="checkbox" name="include_images" value="on"{_checked(bool(effective.get('include_images', True)))}> Include submission images as shortcuts to the master image tree</label>
  <div class="actions"><button type="submit">Save form output</button></div>
</form>
{'<form method="post" action="/admin/exports/clients/' + _esc(slug) + '/forms/' + _esc(form_id) + '/delete" onsubmit="return confirm(\'Reset this form to client defaults?\')"><button class="danger" type="submit">Reset to client defaults</button></form>' if override else ''}
</section>
<p><a href="/admin/exports/{_esc(slug)}">← Back to {_esc(client.get('display_name') or slug)}</a></p>"""
    return _page(f"{slug} / {form_id}", body)


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
    }


def _form_payload(form) -> dict[str, Any]:
    return {
        "fields": form.getlist("fields"),
        "statuses": form.getlist("statuses"),
        "vehicle_types": form.getlist("vehicle_types"),
        "include_images": str(form.get("include_images") or "").lower() in {"on", "true", "1", "yes"},
        "date_from": str(form.get("date_from") or "").strip() or None,
        "date_to": str(form.get("date_to") or "").strip() or None,
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
    return HTMLResponse(_render_root(settings, clients, message=message, error=error))


@router.get("/admin/exports/{slug}", response_class=HTMLResponse)
async def export_client_page(
    slug: str, message: str = "", error: str = "", _admin: str = Depends(verify_admin_auth)
):
    client = await _client_or_404(slug)
    return HTMLResponse(_render_client(client, message=message, error=error))


@router.get("/admin/exports/{slug}/{form_id}", response_class=HTMLResponse)
async def export_client_form_page(
    slug: str,
    form_id: str,
    message: str = "",
    error: str = "",
    _admin: str = Depends(verify_admin_auth),
):
    if not _FORM_RE.fullmatch(form_id):
        raise HTTPException(status_code=404, detail="Form not found")
    client = await _client_or_404(slug)
    available = set(_available_forms(client))
    if form_id not in available:
        raise HTTPException(status_code=404, detail="Form not found")
    return HTMLResponse(_render_form(client, form_id, message=message, error=error))


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
    return RedirectResponse(
        f"/admin/exports/{quote(payload['slug'], safe='')}?message=Export+configuration+created",
        status_code=303,
    )


@router.post("/admin/exports/clients/{slug}")
async def update_export_client(slug: str, request: Request, admin_user: str = Depends(verify_admin_auth)):
    payload = _client_payload(await request.form())
    payload["slug"] = slug
    try:
        await save_client(_pool(), payload, admin_user)
    except (DriveExportError, ValueError) as exc:
        return RedirectResponse(
            f"/admin/exports/{quote(slug, safe='')}?error={quote(str(exc), safe='')}", status_code=303
        )
    return RedirectResponse(
        f"/admin/exports/{quote(slug, safe='')}?message=Client+and+forms+saved", status_code=303
    )


@router.post("/admin/exports/clients/{slug}/forms/{form_id}")
async def save_export_client_form(
    slug: str,
    form_id: str,
    request: Request,
    admin_user: str = Depends(verify_admin_auth),
):
    if not _FORM_RE.fullmatch(form_id):
        raise HTTPException(status_code=400, detail="Invalid form id")
    try:
        await save_client_form(_pool(), slug, form_id, _form_payload(await request.form()), admin_user)
    except (DriveExportError, ValueError) as exc:
        return RedirectResponse(
            f"/admin/exports/{quote(slug, safe='')}/{quote(form_id, safe='')}?error={quote(str(exc), safe='')}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/exports/{quote(slug, safe='')}/{quote(form_id, safe='')}?message=Form+output+saved",
        status_code=303,
    )


@router.post("/admin/exports/clients/{slug}/forms/{form_id}/delete")
async def reset_export_client_form(
    slug: str, form_id: str, admin_user: str = Depends(verify_admin_auth)
):
    if not await delete_client_form(_pool(), slug, form_id, admin_user):
        return RedirectResponse(
            f"/admin/exports/{quote(slug, safe='')}/{quote(form_id, safe='')}?error=No+custom+output+to+reset",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/exports/{quote(slug, safe='')}/{quote(form_id, safe='')}?message=Form+output+reset+to+client+defaults",
        status_code=303,
    )


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
