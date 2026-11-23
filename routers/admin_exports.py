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
:root { --drive-bg:#f8fafd; --surface:#fff; --line:#dadce0; --text:#1f1f1f; --muted:#5f6368; --blue:#0b57d0; --blue-soft:#e8f0fe; --hover:#f1f3f4; }
* { box-sizing:border-box; }
body { margin:0; background:var(--drive-bg); color:var(--text); font:14px/1.45 "Google Sans",Roboto,Arial,sans-serif; }
a { color:var(--blue); text-decoration:none; }
.drive-top { height:64px; background:#fff; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:16px; padding:0 20px; position:sticky; top:0; z-index:5; }
.drive-brand { display:flex; align-items:center; gap:8px; font-size:20px; font-weight:500; color:#5f6368; white-space:nowrap; }
.drive-brand svg { width:32px; height:32px; }
.drive-search { flex:1; max-width:720px; background:#f1f3f4; border-radius:24px; display:flex; align-items:center; padding:0 16px; height:44px; }
.drive-search input { width:100%; border:0; background:transparent; outline:none; font-size:15px; color:var(--text); margin:0; padding:0; }
.drive-shell { display:grid; grid-template-columns:220px minmax(0,1fr); min-height:calc(100vh - 64px); }
.drive-side { padding:12px 8px; background:#fff; border-right:1px solid var(--line); }
.drive-side a { display:flex; align-items:center; gap:10px; padding:8px 14px; border-radius:999px; color:#3c4043; font-weight:500; }
.drive-side a.active { background:var(--blue-soft); color:var(--blue); }
.drive-side a:hover { background:var(--hover); }
.drive-main { padding:18px 24px 40px; min-width:0; }
.drive-toolbar { display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
.crumbs { display:flex; gap:6px; align-items:center; color:var(--muted); font-size:13px; flex-wrap:wrap; }
.crumbs a { color:var(--muted); } .crumbs a:hover { color:var(--blue); }
.drive-title { font-size:20px; font-weight:500; margin:8px 0 2px; }
.drive-sub { color:var(--muted); font-size:13px; margin:0 0 14px; }
.flash { padding:10px 14px; border-radius:8px; margin:10px 0; font-size:13px; }
.flash.ok { background:#e6f4ea; color:#137333; } .flash.bad { background:#fce8e6; color:#c5221f; }
.drive-layout { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:18px; align-items:start; }
.file-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:12px; }
.drive-card { display:flex; flex-direction:column; gap:6px; padding:14px; background:var(--surface); border:1px solid var(--line); border-radius:12px; color:var(--text); min-height:120px; transition:box-shadow .15s,border-color .15s; }
.drive-card:hover { border-color:#c6d4f7; box-shadow:0 1px 4px rgba(60,64,67,.25); }
.drive-icon { width:38px; height:38px; display:grid; place-items:center; }
.drive-icon svg { width:34px; height:34px; }
.drive-name { font-weight:600; font-size:14px; word-break:break-word; }
.drive-meta { font-size:12px; color:var(--muted); word-break:break-word; }
.drive-empty { padding:28px; background:#fff; border:1px dashed var(--line); border-radius:12px; color:var(--muted); font-size:13px; }
.details { background:#fff; border:1px solid var(--line); border-radius:12px; padding:16px; position:sticky; top:80px; max-height:calc(100vh - 96px); overflow:auto; }
.details h2 { font-size:15px; margin:0 0 8px; font-weight:600; }
.details h3 { font-size:13px; margin:14px 0 6px; color:#3c4043; }
.details .hint,.details .muted { color:var(--muted); font-size:12px; }
.details-list { display:flex; flex-direction:column; gap:6px; margin:8px 0 14px; }
.details-list a { display:flex; justify-content:space-between; gap:8px; padding:8px 10px; border:1px solid var(--line); border-radius:8px; color:var(--text); }
.details-list a:hover { background:var(--hover); }
.details .card { border:0; padding:0; margin:0; background:transparent; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
fieldset { border:1px solid var(--line); border-radius:8px; padding:10px; margin:10px 0; }
legend { font-weight:600; color:#3c4043; font-size:12px; padding:0 4px; }
label { display:block; margin:8px 0; color:var(--muted); font-size:12px; }
label.check { color:var(--text); margin:4px 0; font-size:12px; }
input,select,textarea { display:block; width:100%; margin-top:4px; padding:8px 10px; border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--text); font:13px Roboto,Arial,sans-serif; }
label.check input { display:inline; width:auto; margin-right:6px; }
button,.button { border:1px solid transparent; border-radius:999px; background:var(--blue); color:#fff; padding:8px 16px; cursor:pointer; font-weight:500; font-size:13px; }
button.danger { background:#fff; border-color:#c5221f; color:#c5221f; }
.actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
pre { overflow:auto; max-height:220px; background:#f1f3f4; padding:10px; border-radius:8px; font-size:12px; }
.drive-settings summary { cursor:pointer; color:#3c4043; font-weight:500; font-size:13px; margin:12px 0; }
.drive-settings summary:hover { color:var(--blue); }
.form-panel { background:#fff; border:1px solid var(--line); border-radius:12px; padding:18px; }
@media(max-width:980px) { .drive-shell { grid-template-columns:1fr; } .drive-side { display:none; } .drive-layout { grid-template-columns:1fr; } .details { position:static; max-height:none; } .drive-top { padding:0 10px; } .drive-main { padding:14px 12px 30px; } }
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


FOLDER_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#5f6368" d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg>'
FORM_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#4285f4" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm-1 7V3.5L18.5 9H13z"/><path fill="#a8c7fa" d="M7 13h10v1.5H7zm0 4h7v1.5H7z"/></svg>'
DRIVE_LOGO = '<svg viewBox="0 0 87.3 78" aria-hidden="true"><path fill="#0066da" d="M6.6 66.85l3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3L27.5 53H0c0 1.55.4 3.1 1.2 4.5z"/><path fill="#00ac47" d="M43.65 25L29.9 1.2c-1.35.8-2.5 1.9-3.3 3.3L1.2 48.5A9.2 9.2 0 0 0 0 53h27.5z"/><path fill="#ea4335" d="M73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75L86.1 57.5c.8-1.4 1.2-2.95 1.2-4.5H59.8l5.85 11.5z"/><path fill="#00832d" d="M43.65 25L57.4 1.2C56.05.4 54.5 0 52.9 0H34.4c-1.6 0-3.15.45-4.5 1.2z"/><path fill="#2684fc" d="M59.8 53H27.5L13.75 76.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z"/><path fill="#ffba00" d="M73.4 26.5L60.7 4.5c-.8-1.4-1.95-2.5-3.3-3.3L43.65 25l16.15 28h27.45c0-1.55-.4-3.1-1.2-4.5z"/></svg>'


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"""


def _crumbs(items: list[tuple[str, str | None]]) -> str:
    parts = []
    for label, href in items:
        if href:
            parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
        else:
            parts.append(f"<span>{_esc(label)}</span>")
    return '<nav class="crumbs">' + " <span>›</span> ".join(parts) + "</nav>"


def _drive_card(href: str, name: str, meta: str, icon: str = "folder") -> str:
    icon_svg = FORM_ICON if icon == "form" else FOLDER_ICON
    return (
        f'<a class="drive-card" href="{_esc(href)}" data-drive-name="{_esc(name)} {_esc(meta)}">'
        f'<span class="drive-icon">{icon_svg}</span>'
        f'<span class="drive-name">{_esc(name)}</span>'
        f'<span class="drive-meta">{_esc(meta)}</span></a>'
    )


def _kind_label(settings: dict[str, Any], kind: str) -> str:
    key = "internal_folder_name" if kind == "internal" else "client_folder_name"
    return str(settings.get(key) or ("Internal Exports" if kind == "internal" else "Client Exports"))


def _sidebar(active: str) -> str:
    items = [
        ("drive", "/admin/exports", "My Drive", FOLDER_ICON),
        ("client", "/admin/exports/client-exports", "Client Exports", FOLDER_ICON),
        ("internal", "/admin/exports/internal-exports", "Internal Exports", FOLDER_ICON),
    ]
    links = "".join(
        f'<a href="{_esc(href)}" class="{"active" if active == key else ""}">{icon}{_esc(label)}</a>'
        for key, href, label, icon in items
    )
    return f'<aside class="drive-side">{links}</aside>'


def _drive_shell(
    title: str,
    crumbs: str,
    heading: str,
    subtitle: str,
    content: str,
    details: str,
    *,
    active: str = "drive",
    message: str = "",
    error: str = "",
    toolbar_actions: str = "",
) -> str:
    body = f"""
<header class="drive-top">
  <a class="drive-brand" href="/admin/exports">{DRIVE_LOGO}<span>Drive</span></a>
  <div class="drive-search"><input type="search" id="driveSearch" placeholder="Tìm trong Drive" aria-label="Tìm trong Drive" oninput="filterDriveCards()"></div>
</header>
<div class="drive-shell">
  {_sidebar(active)}
  <main class="drive-main">
    <div class="drive-toolbar">{crumbs}<div class="actions">{toolbar_actions}</div></div>
    <h1 class="drive-title">{_esc(heading)}</h1>
    <p class="drive-sub">{_esc(subtitle)}</p>
    {_flash(message, error)}
    <div class="drive-layout">
      <section class="drive-content" id="driveFiles">{content or '<div class="drive-empty">Thư mục này trống.</div>'}</section>
      <aside class="details">{details}</aside>
    </div>
  </main>
</div>
<script>
function filterDriveCards() {{
  const q = (document.getElementById('driveSearch').value || '').trim().toLowerCase();
  document.querySelectorAll('[data-drive-name]').forEach(card => {{
    card.hidden = q && !card.dataset.driveName.toLowerCase().includes(q);
  }});
}}
</script>"""
    return _page(title, body)


def _client_form(
    client: dict[str, Any] | None = None, *, create: bool = False, default_kind: str = "client"
) -> str:
    raw = dict(client or {})
    slug = str(raw.get("slug") or "")
    kind = str(raw.get("kind") or default_kind)
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
      <p class="hint">No selection means every active form. Open a form folder to edit its output.</p>
      {_checkbox_group('form_ids', [(fid, fid) for fid in forms], selected_forms)}
    </fieldset>
    <div class="actions"><button type="submit">{'Create export' if create else 'Save client and form selection'}</button></div>
  </form>
</article>"""


def _form_cards(client: dict[str, Any]) -> str:
    overrides = client.get("form_settings") or {}
    selected = sorted(set(str(v) for v in (client.get("form_ids") or [])))
    form_ids = selected or sorted(set(FORM_REGISTRY) | set(overrides))
    if not form_ids:
        return '<div class="drive-empty">Chưa có biểu mẫu nào đang hoạt động.</div>'
    cards = []
    for form_id in form_ids:
        schema = FORM_REGISTRY.get(form_id)
        title = str(getattr(schema, "title", "") or form_id)
        state = "custom output" if form_id in overrides else "client defaults"
        cards.append(_drive_card(
            f"/admin/exports/{_esc(client['slug'])}/{_esc(form_id)}", title,
            f"{form_id} · {state}", "form",
        ))
    return '<div class="file-grid">' + "".join(cards) + "</div>"


def _client_cards(clients: list[dict[str, Any]]) -> str:
    if not clients:
        return '<div class="drive-empty">Chưa có cấu hình xuất nào.</div>'
    return '<div class="file-grid">' + "".join(
        _drive_card(
            f"/admin/exports/{_esc(client['slug'])}",
            str(client.get("display_name") or client["slug"]),
            f"{client['slug']} · {len(client.get('form_ids') or []) or 'all'} forms",
        )
        for client in clients
    ) + "</div>"


def _date_mode(override: dict[str, Any], key: str) -> str:
    if override.get(f"{key}_clear") is True:
        return "clear"
    return "custom" if override.get(key) else "inherit"


def _date_controls(override: dict[str, Any], key: str, label: str, client_value: str) -> str:
    mode = _date_mode(override, key)
    inherited = _esc(client_value or "any")
    return f"""<fieldset><legend>{label}</legend>
<label class="check"><input type="radio" name="{key}_mode" value="inherit"{_checked(mode == 'inherit')}> Inherit client range ({inherited})</label>
<label class="check"><input type="radio" name="{key}_mode" value="clear"{_checked(mode == 'clear')}> No {label.lower()} limit</label>
<label class="check"><input type="radio" name="{key}_mode" value="custom"{_checked(mode == 'custom')}> Custom date</label>
<label>Date<input type="date" name="{key}" value="{_esc(override.get(key) or '')}"></label>
</fieldset>"""


def _settings_details(settings: dict[str, Any], report_text: str) -> str:
    return f"""
<h2>Drive settings</h2>
<details class="drive-settings" open><summary>Folder names</summary>
<form method="post" action="/admin/exports/settings" onsubmit="return confirm('Save Drive folder names?')"><div class="grid">
<label>Master export folder<input name="master_folder_name" value="{_esc(settings.get('master_folder_name', 'Master Export'))}" required></label>
<label>Client exports folder<input name="client_folder_name" value="{_esc(settings.get('client_folder_name', 'Client Exports'))}" required></label>
<label>Internal exports folder<input name="internal_folder_name" value="{_esc(settings.get('internal_folder_name', 'Internal Exports'))}" required></label>
<label>Images master folder<input name="images_folder_name" value="{_esc(settings.get('images_folder_name', 'Images Master'))}" required></label>
</div><div class="actions"><button type="submit">Save folder names</button></div></form>
</details>
<details class="drive-settings"><summary>Last export report</summary><pre>{_esc(report_text)}</pre></details>"""


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
    client_count = sum(1 for c in clients if str(c.get("kind")) == "client")
    internal_count = sum(1 for c in clients if str(c.get("kind")) == "internal")
    folders = "".join([
        _drive_card("/admin/exports/client-exports", _kind_label(settings, "client"), f"{client_count} client folders"),
        _drive_card("/admin/exports/internal-exports", _kind_label(settings, "internal"), f"{internal_count} internal folders"),
    ])
    run_form = '<form method="post" action="/admin/exports/run" onsubmit="return confirm(\'Run Google Drive export now?\')"><button type="submit">Run export</button></form>'
    return _drive_shell(
        "Drive Exports", _crumbs([("My Drive", None)]), "My Drive",
        "Google Drive export folders and controls.",
        f'<div class="file-grid">{folders}</div>',
        _settings_details(settings, report_text),
        active="drive", message=message, error=error, toolbar_actions=run_form,
    )


def _render_client_exports(
    settings: dict[str, Any], clients: list[dict[str, Any]], kind: str,
    *, message: str = "", error: str = "",
) -> str:
    entries = [c for c in clients if str(c.get("kind")) == kind]
    label = _kind_label(settings, kind)
    noun = "client" if kind == "client" else "internal export"
    links = "".join(
        f'<a href="/admin/exports/{_esc(client["slug"])}"><span>{_esc(client.get("display_name") or client["slug"])}</span><span class="muted">{_esc(client["slug"])}</span></a>'
        for client in entries
    )
    details = f"""
<h2>Edit {noun}s</h2>
<p class="hint">Chọn một thư mục để quản lý form, hoặc tạo cấu hình mới.</p>
<div class="details-list">{links or '<span class="muted">Chưa có cấu hình.</span>'}</div>
<details class="drive-settings" id="newClientForm" open><summary>New {noun}</summary>
{_client_form(create=True, default_kind=kind)}
</details>"""
    toolbar = f'<button type="button" onclick="document.getElementById(\'newClientForm\').open=true;document.getElementById(\'newClientForm\').scrollIntoView({{behavior:\'smooth\'}})">New {noun}</button>'
    return _drive_shell(
        label, _crumbs([("My Drive", "/admin/exports"), (label, None)]), label,
        f"Quản lý {noun} và các thư mục form của họ.",
        _client_cards(entries), details, active=kind, message=message, error=error,
        toolbar_actions=toolbar,
    )


def _render_client(
    client: dict[str, Any], *, message: str = "", error: str = "",
    settings: dict[str, Any] | None = None,
) -> str:
    settings = settings or {}
    slug = str(client.get("slug") or "")
    kind = str(client.get("kind") or "client")
    label = _kind_label(settings, kind)
    danger = f"""
<form method="post" action="/admin/exports/clients/{_esc(slug)}/delete" onsubmit="return confirm('Delete {_esc(slug)}? Drive folders are kept.')">
<button class="danger" type="submit">Delete configuration</button></form>"""
    details = f"""
<h2>Manage this client's forms</h2>
<p class="hint">Chọn các form khách hàng nhận và cấu hình mặc định của thư mục.</p>
{_client_form(client)}
<div style="border-top:1px solid var(--line);margin-top:14px;padding-top:10px;">{danger}</div>"""
    return _drive_shell(
        f"Export {slug}",
        _crumbs([("My Drive", "/admin/exports"), (label, f"/admin/exports/{'client-exports' if kind == 'client' else 'internal-exports'}"), (str(client.get("display_name") or slug), None)]),
        str(client.get("display_name") or slug),
        f"{label} · {len(client.get('form_ids') or []) or 'all'} forms · {slug}",
        _form_cards(client), details, active=kind, message=message, error=error,
    )


def _render_form(
    client: dict[str, Any],
    form_id: str,
    *,
    message: str = "",
    error: str = "",
    settings: dict[str, Any] | None = None,
) -> str:
    settings = settings or {}
    slug = str(client.get("slug") or "")
    kind = str(client.get("kind") or "client")
    label = _kind_label(settings, kind)
    override = (client.get("form_settings") or {}).get(form_id) or {}
    effective = effective_form_config(client, form_id)
    selected_forms = set(str(v) for v in (client.get("form_ids") or []))
    warning = ""
    if selected_forms and form_id not in selected_forms:
        warning = '<div class="flash bad">This form is not selected for the client yet. Saving this form adds it to the client.</div>'
    date_hint = (
        f"Inherit uses the client range "
        f"({_esc(client.get('date_from') or 'any')} to {_esc(client.get('date_to') or 'any')}); "
        "Custom requires a date."
    )
    reset = (
        f'<form method="post" action="/admin/exports/clients/{_esc(slug)}/forms/{_esc(form_id)}/delete" '
        f'onsubmit="return confirm(\'Reset this form to client defaults?\')"><button class="danger" type="submit">Reset to client defaults</button></form>'
        if override else ""
    )
    panel = f"""
<section class="form-panel">
<h2>What this client can access</h2>
<p class="hint">Các thiết lập này chỉ áp dụng cho <b>{_esc(client.get('display_name') or slug)}</b> / <code>{_esc(form_id)}</code>.</p>
{_flash(message, error)}{warning}
<form method="post" action="/admin/exports/clients/{_esc(slug)}/forms/{_esc(form_id)}" onsubmit="return confirm('Save output settings for {_esc(slug)} / {_esc(form_id)}?')">
  <div class="grid">
    <fieldset><legend>Fields</legend><p class="hint">Unchecking everything falls back to the client field list.</p>{_checkbox_group('fields', list(EXPORT_FIELDS), list(effective.get('fields') or []))}</fieldset>
    <fieldset><legend>Statuses</legend>{_checkbox_group('statuses', [(status, status) for status in EXPORT_STATUSES], list(effective.get('statuses') or []))}</fieldset>
    <fieldset><legend>Vehicle kinds</legend><p class="hint">No selection means all vehicles.</p>{_checkbox_group('vehicle_types', [(kind, kind) for kind in VEHICLE_TYPES], list(effective.get('vehicle_types') or []))}</fieldset>
  </div>
  <div class="grid">
    {_date_controls(override, 'date_from', 'From date', str(client.get('date_from') or ''))}
    {_date_controls(override, 'date_to', 'To date', str(client.get('date_to') or ''))}
  </div>
  <p class="hint">{date_hint} Choose <b>No limit</b> to override a client date range with an unbounded form export.</p>
  <label class="check"><input type="checkbox" name="include_images" value="on"{_checked(bool(effective.get('include_images', True)))}> Include submission images as shortcuts to the master image tree</label>
  <div class="actions"><button type="submit">Save form output</button></div>
</form>
</section>"""
    details = f"""
<h2>Folder details</h2>
<p><b>Client:</b> {_esc(client.get('display_name') or slug)}</p>
<p><b>Form:</b> <code>{_esc(form_id)}</code></p>
<p><b>Fields:</b> {len(effective.get('fields') or [])} · <b>Statuses:</b> {len(effective.get('statuses') or [])}</p>
<p><b>Images:</b> {'yes' if effective.get('include_images') else 'no'}</p>
{reset}"""
    return _drive_shell(
        f"{slug} / {form_id}",
        _crumbs([("My Drive", "/admin/exports"), (label, f"/admin/exports/{'client-exports' if kind == 'client' else 'internal-exports'}"), (str(client.get('display_name') or slug), f"/admin/exports/{slug}"), (form_id, None)]),
        form_id,
        f"Quyền truy cập của {client.get('display_name') or slug} trong form này.",
        panel, details, active=kind, message=message, error=error,
    )


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
        "date_from_mode": str(form.get("date_from_mode") or "inherit"),
        "date_to_mode": str(form.get("date_to_mode") or "inherit"),
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


@router.get("/admin/exports/client-exports", response_class=HTMLResponse)
async def client_exports_page(
    message: str = "", error: str = "", _admin: str = Depends(verify_admin_auth)
):
    pool = _pool()
    try:
        settings = await get_settings(pool)
        clients = await list_clients(pool)
    except Exception as exc:
        logger.warning("client exports page query failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Drive export database unavailable.") from exc
    return HTMLResponse(_render_client_exports(settings, clients, "client", message=message, error=error))


@router.get("/admin/exports/internal-exports", response_class=HTMLResponse)
async def internal_exports_page(
    message: str = "", error: str = "", _admin: str = Depends(verify_admin_auth)
):
    pool = _pool()
    try:
        settings = await get_settings(pool)
        clients = await list_clients(pool)
    except Exception as exc:
        logger.warning("internal exports page query failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Drive export database unavailable.") from exc
    return HTMLResponse(_render_client_exports(settings, clients, "internal", message=message, error=error))


@router.get("/admin/exports/{slug}", response_class=HTMLResponse)
async def export_client_page(
    slug: str, message: str = "", error: str = "", _admin: str = Depends(verify_admin_auth)
):
    client = await _client_or_404(slug)
    settings = await get_settings(_pool())
    return HTMLResponse(_render_client(client, message=message, error=error, settings=settings))


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
    settings = await get_settings(_pool())
    return HTMLResponse(_render_form(client, form_id, message=message, error=error, settings=settings))


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
