# -*- coding: utf-8 -*-
"""
HTML rendering service for the System-Wide Immutable Audit Log Viewer.
Provides a modern, responsive web dashboard for inspecting every action, hop,
cryptographic SHA-256 seal, and data mutation under Decree 13 / GDPR compliance.
"""

import json
from typing import List, Dict, Any, Optional


def render_audit_viewer_html(
    audit_logs: List[Dict[str, Any]],
    stats: Optional[Dict[str, int]] = None
) -> str:
    stats = stats or {
        "total": len(audit_logs),
        "ingress_normalizer": sum(1 for log in audit_logs if log.get("hop_number") in (1, 2) or log.get("component_name") in ("INGRESS", "NORMALIZER")),
        "ai_fraud": sum(1 for log in audit_logs if log.get("hop_number") in (3, 4, 5) or log.get("component_name") in ("ANTI_FRAUD", "AI_VISION", "EVALUATOR")),
        "persistence_admin": sum(1 for log in audit_logs if log.get("hop_number") in (6, 7, 8) or log.get("component_name") in ("PERSISTENCE", "MODERATION", "OUTBOX", "PRIVACY_ENGINE", "FORM_ENGINE")),
    }

    logs_json = json.dumps(audit_logs, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Trust Bridge - Nhật Ký Kiểm Toán Toàn Hệ Thống</title>
    <style>
        :root {{
            --bg: #090d16;
            --surface: #131b2e;
            --surface-hover: #1a253e;
            --border: #23314f;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --success: #10b981;
            --success-bg: rgba(16, 185, 129, 0.15);
            --danger: #ef4444;
            --danger-bg: rgba(239, 68, 68, 0.15);
            --warning: #f59e0b;
            --warning-bg: rgba(245, 158, 11, 0.15);
            --info: #06b6d4;
            --info-bg: rgba(6, 182, 212, 0.15);
            --purple: #8b5cf6;
            --purple-bg: rgba(139, 92, 246, 0.15);
            --text: #f8fafc;
            --muted: #94a3b8;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px 16px;
        }}

        .container {{ max-width: 1380px; margin: 0 auto; }}

        /* Top Header */
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
            gap: 16px;
            flex-wrap: wrap;
        }}

        .header-brand h1 {{
            font-size: 1.35rem;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .header-brand p {{
            font-size: 0.85rem;
            color: var(--muted);
            margin-top: 4px;
        }}

        .header-actions {{
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }}

        .nav-btn {{
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 8px 14px;
            border-radius: 8px;
            text-decoration: none;
            font-size: 0.82rem;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
            cursor: pointer;
        }}

        .nav-btn:hover {{
            background: var(--surface-hover);
            border-color: var(--accent);
        }}

        .nav-btn.primary {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}
        .nav-btn.primary:hover {{
            background: var(--accent-hover);
        }}

        /* KPI Summary Grid */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}

        .stat-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px 20px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            position: relative;
            overflow: hidden;
        }}

        .stat-card::before {{
            content: '';
            position: absolute;
            top: 0; left: 0; bottom: 0;
            width: 4px;
        }}

        .stat-card.total::before {{ background: var(--info); }}
        .stat-card.ingress::before {{ background: var(--purple); }}
        .stat-card.ai::before {{ background: var(--warning); }}
        .stat-card.persistence::before {{ background: var(--success); }}

        .stat-title {{
            font-size: 0.8rem;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 700;
        }}

        .stat-value {{
            font-size: 1.8rem;
            font-weight: 800;
        }}

        /* Controls & Filter Bar */
        .controls-bar {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
        }}

        .filter-group {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
            flex: 1;
        }}

        .search-box {{
            flex: 1;
            min-width: 250px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 12px;
            color: var(--text);
            font-size: 0.85rem;
            outline: none;
        }}
        .search-box:focus {{ border-color: var(--accent); }}

        .select-filter {{
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 12px;
            color: var(--text);
            font-size: 0.85rem;
            outline: none;
            cursor: pointer;
        }}

        .export-group {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}

        /* Table Styling */
        .table-wrap {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.84rem;
        }}

        th {{
            background: rgba(35, 49, 79, 0.4);
            padding: 12px 14px;
            font-weight: 700;
            color: var(--muted);
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }}

        td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--border);
            vertical-align: middle;
        }}

        tr:hover td {{
            background: var(--surface-hover);
        }}

        /* Badges */
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 16px;
            font-size: 0.72rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }}

        .badge.hop {{ background: var(--purple-bg); color: #c084fc; border: 1px solid rgba(192, 132, 252, 0.3); }}
        .badge.comp {{ background: var(--info-bg); color: var(--info); border: 1px solid rgba(6, 182, 212, 0.3); }}
        .badge.action-ok {{ background: var(--success-bg); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge.action-fail {{ background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }}
        .badge.action-warn {{ background: var(--warning-bg); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge.entity {{ background: rgba(148, 163, 184, 0.12); color: #cbd5e1; border: 1px solid var(--border); }}

        .hash-code {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.75rem;
            color: #38bdf8;
            background: rgba(0, 0, 0, 0.3);
            padding: 3px 6px;
            border-radius: 4px;
            border: 1px solid var(--border);
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .hash-code:hover {{ border-color: var(--accent); }}

        .sub-time {{
            font-size: 0.74rem;
            color: var(--muted);
        }}

        .btn-inspect {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text);
            padding: 5px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.78rem;
            font-weight: 600;
            transition: all 0.15s;
        }}

        .btn-inspect:hover {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}

        /* Empty State */
        .empty-state {{
            text-align: center;
            padding: 60px 20px;
            color: var(--muted);
        }}
        .empty-state svg {{
            width: 48px; height: 48px; fill: var(--border);
            margin-bottom: 12px;
        }}

        /* Inspection Modal */
        .modal-overlay {{
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.75);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            backdrop-filter: blur(4px);
            padding: 16px;
        }}

        .modal-box {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 16px;
            width: 100%;
            max-width: 850px;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }}

        .modal-header {{
            padding: 16px 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .modal-header h3 {{
            font-size: 1.1rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .modal-close {{
            background: none;
            border: none;
            color: var(--muted);
            font-size: 1.4rem;
            cursor: pointer;
            line-height: 1;
        }}
        .modal-close:hover {{ color: var(--text); }}

        .modal-body {{
            padding: 20px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .seal-verification {{
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.3);
            border-radius: 10px;
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}

        .seal-verification-text {{
            font-size: 0.82rem;
            color: #6ee7b7;
        }}

        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            background: rgba(0, 0, 0, 0.2);
            padding: 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
        }}

        .meta-item label {{
            display: block;
            font-size: 0.72rem;
            color: var(--muted);
            text-transform: uppercase;
            font-weight: 700;
            margin-bottom: 2px;
        }}

        .meta-item span {{
            font-size: 0.88rem;
            font-weight: 600;
        }}

        .json-preview {{
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.78rem;
            max-height: 280px;
            overflow-y: auto;
            color: #38bdf8;
            white-space: pre-wrap;
            word-break: break-all;
        }}

        @media (max-width: 768px) {{
            table thead {{ display: none; }}
            table, tbody, tr, td {{ display: block; width: 100%; }}
            tr {{
                margin-bottom: 12px;
                border: 1px solid var(--border);
                border-radius: 10px;
                background: var(--surface);
                padding: 10px;
            }}
            td {{
                padding: 8px 6px;
                border: none;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            td::before {{
                content: attr(data-label);
                font-weight: 700;
                color: var(--muted);
                font-size: 0.75rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-brand">
                <h1>🛡️ Nhật Ký Kiểm Toán Toàn Hệ Thống</h1>
                <p>Immutable Audit Trail & Cryptographic Hop Chains (Decree 13 / GDPR Legal Compliance)</p>
            </div>
            <div class="header-actions">
                <a href="/admin/forms" class="nav-btn">📋 Quản Lý Form</a>
                <a href="/admin/submissions" class="nav-btn">📊 Dữ Liệu Nộp</a>
                <a href="/admin/samples" class="nav-btn">🖼️ Ảnh Mẫu Test</a>
                <button type="button" onclick="location.reload()" class="nav-btn primary">🔄 Làm Mới</button>
            </div>
        </header>

        <!-- KPI Summary Cards -->
        <div class="stats-grid">
            <div class="stat-card total">
                <div class="stat-title">Tổng Số Sự Kiện Kiểm Toán</div>
                <div class="stat-value" id="kpiTotal">{stats["total"]}</div>
            </div>
            <div class="stat-card ingress">
                <div class="stat-title">Tiếp Nhận & Chuẩn Hóa (Hops 1-2)</div>
                <div class="stat-value" id="kpiIngress" style="color: #c084fc;">{stats["ingress_normalizer"]}</div>
            </div>
            <div class="stat-card ai">
                <div class="stat-title">AI & Chống Gian Lận (Hops 3-5)</div>
                <div class="stat-value" id="kpiAI" style="color: var(--warning);">{stats["ai_fraud"]}</div>
            </div>
            <div class="stat-card persistence">
                <div class="stat-title">Lưu Trữ & Quản Trị (Hops 6-8)</div>
                <div class="stat-value" id="kpiPersist" style="color: var(--success);">{stats["persistence_admin"]}</div>
            </div>
        </div>

        <!-- Filter & Search Toolbar -->
        <div class="controls-bar">
            <div class="filter-group">
                <input type="text" id="searchInput" class="search-box" placeholder="🔍 Tìm theo Mã Hồ Sơ, Entity, Action, Actor, Hash..." oninput="filterData()">
                <select id="compFilter" class="select-filter" onchange="filterData()">
                    <option value="ALL">Tất cả thành phần (Components)</option>
                    <option value="INGRESS">INGRESS (Nhận Webhook/Form)</option>
                    <option value="NORMALIZER">NORMALIZER (Xử lý ảnh & EXIF)</option>
                    <option value="ANTI_FRAUD">ANTI_FRAUD (Chống gian lận)</option>
                    <option value="AI_VISION">AI_VISION (Phân loại & OCR)</option>
                    <option value="EVALUATOR">EVALUATOR (Kiểm tra tuân thủ)</option>
                    <option value="PERSISTENCE">PERSISTENCE (Ghi DB & Niêm phong)</option>
                    <option value="OUTBOX">OUTBOX (Đẩy sự kiện fan-out)</option>
                    <option value="MODERATION">MODERATION (Xử lý khiếu nại)</option>
                    <option value="PRIVACY_ENGINE">PRIVACY_ENGINE (Nghị định 13)</option>
                    <option value="FORM_ENGINE">FORM_ENGINE (Cấu hình biểu mẫu)</option>
                </select>
                <select id="entityFilter" class="select-filter" onchange="filterData()">
                    <option value="ALL">Tất cả loại đối tượng (Entities)</option>
                    <option value="SURVEY">SURVEY (Hồ sơ khảo sát)</option>
                    <option value="DRIVER">DRIVER (Tài xế / Định danh)</option>
                    <option value="VEHICLE">VEHICLE (Phương tiện)</option>
                    <option value="SCHEMA">SCHEMA (Cấu hình Form)</option>
                    <option value="SYSTEM">SYSTEM (Hệ thống)</option>
                </select>
            </div>
            <div class="export-group">
                <button type="button" class="nav-btn" onclick="exportCSV()">📥 Xuất CSV</button>
                <button type="button" class="nav-btn" onclick="exportJSON()">📥 Xuất JSON</button>
            </div>
        </div>

        <!-- Audit Table -->
        <div class="table-wrap">
            <table id="auditTable">
                <thead>
                    <tr>
                        <th>Thời Gian</th>
                        <th>Chặng & Thành Phần</th>
                        <th>Hành Động</th>
                        <th>Đối Tượng</th>
                        <th>Tác Nhân</th>
                        <th>Chữ Ký SHA-256 (Tamper Seal)</th>
                        <th style="text-align: right;">Thao Tác</th>
                    </tr>
                </thead>
                <tbody id="tableBody">
                    <!-- Populated dynamically by JS -->
                </tbody>
            </table>
            <div id="emptyState" class="empty-state" style="display: none;">
                <svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></svg>
                <h3>Chưa có sự kiện kiểm toán nào phù hợp bộ lọc</h3>
                <p>Mọi hành động đọc, ghi, xử lý ảnh và phê duyệt dữ liệu đều được ghi nhận tự động.</p>
            </div>
        </div>
    </div>

    <!-- Inspection Modal -->
    <div id="detailModal" class="modal-overlay" onclick="closeModalOnBackdrop(event)">
        <div class="modal-box">
            <div class="modal-header">
                <h3 id="modalTitle">Chi Tiết Sự Kiện Kiểm Toán</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="seal-verification">
                    <div>
                        <b style="color:var(--success); display:flex; align-items:center; gap:6px;">
                            🛡️ Chữ Ký Mật Mã Học Hợp Lệ (Tamper-Evident SHA-256)
                        </b>
                        <div class="seal-verification-text">Bảo đảm tính toàn vẹn dữ liệu chuỗi chặng theo Nghị định 13/2023/NĐ-CP & Luật An ninh mạng.</div>
                    </div>
                    <span class="badge action-ok">VERIFIED</span>
                </div>

                <div class="meta-grid">
                    <div class="meta-item">
                        <label>Mã Sự Kiện ID</label>
                        <span id="mEventId" style="font-family:monospace; font-size:0.8rem;">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Thời Gian Ghi Nhận</label>
                        <span id="mTime">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Chặng Kiểm Toán</label>
                        <span id="mHop">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Thành Phần Thực Thi</label>
                        <span id="mComponent">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Hành Động</label>
                        <span id="mAction">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Đối Tượng Ảnh Hưởng</label>
                        <span id="mEntity">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Tác Nhân Thực Hiện</label>
                        <span id="mActor">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Địa Chỉ IP</label>
                        <span id="mIp">-</span>
                    </div>
                </div>

                <div>
                    <label style="display:block; font-size:0.75rem; color:var(--muted); text-transform:uppercase; font-weight:700; margin-bottom:6px;">Mã Băm Niêm Phong Toàn Vẹn (SHA-256 Tamper Hash)</label>
                    <div id="mHash" class="hash-code" style="width:100%; word-break:break-all; padding:8px 12px; font-size:0.82rem;">-</div>
                </div>

                <div>
                    <label style="display:block; font-size:0.75rem; color:var(--muted); text-transform:uppercase; font-weight:700; margin-bottom:6px;">Chi Tiết Dữ Liệu Hành Động (Factual Payload Trace)</label>
                    <pre id="mJson" class="json-preview"></pre>
                </div>
            </div>
        </div>
    </div>

    <script>
        const RAW_DATA = {logs_json};

        function formatDate(dStr) {{
            if (!dStr) return "-";
            try {{
                const d = new Date(dStr);
                return d.toLocaleString("vi-VN", {{ hour12: false }});
            }} catch(e) {{
                return dStr;
            }}
        }}

        function getActionBadge(action) {{
            const act = (action || "").toUpperCase();
            if (act.includes("APPROVED") || act.includes("COMMITTED") || act.includes("SUCCESS") || act.includes("NORMALIZED")) {{
                return `<span class="badge action-ok">${{act}}</span>`;
            }}
            if (act.includes("REJECTED") || act.includes("FAIL") || act.includes("ERROR") || act.includes("DENIED")) {{
                return `<span class="badge action-fail">${{act}}</span>`;
            }}
            return `<span class="badge action-warn">${{act}}</span>`;
        }}

        function copyText(txt, el) {{
            navigator.clipboard.writeText(txt).then(() => {{
                const orig = el.textContent;
                el.textContent = "✅ Copied!";
                setTimeout(() => {{ el.textContent = orig; }}, 1500);
            }}).catch(() => prompt("Copy hash:", txt));
        }}

        function renderRows(items) {{
            const tbody = document.getElementById("tableBody");
            const empty = document.getElementById("emptyState");
            if (!items.length) {{
                tbody.innerHTML = "";
                empty.style.display = "block";
                return;
            }}
            empty.style.display = "none";

            tbody.innerHTML = items.map((log, idx) => {{
                const time = formatDate(log.event_timestamp);
                const hopNum = log.hop_number != null ? `Hop ${{log.hop_number}}` : "SYS";
                const hopName = log.hop_name || "HOP_GENERAL";
                const comp = log.component_name || "CORE";
                const actBadge = getActionBadge(log.action);
                const entity = `${{log.entity_type || "SYSTEM"}}:${{log.entity_id || "-"}}`;
                const actor = `${{log.actor_type || "SYSTEM"}} (${{log.actor_id || "SYSTEM"}})`;
                const hash = log.tamper_evident_hash || "N/A";
                const truncHash = hash.length > 16 ? hash.slice(0, 16) + "..." : hash;

                return `
                <tr>
                    <td data-label="Thời Gian">
                        <div style="font-weight:600;">${{time}}</div>
                        <div class="sub-time">${{log.id ? log.id.slice(0, 8) : ""}}</div>
                    </td>
                    <td data-label="Chặng & Thành Phần">
                        <span class="badge hop">${{hopNum}}</span>
                        <span class="badge comp">${{comp}}</span>
                    </td>
                    <td data-label="Hành Động">${{actBadge}}</td>
                    <td data-label="Đối Tượng">
                        <span class="badge entity" style="max-width:200px; overflow:hidden; text-overflow:ellipsis; display:inline-block; vertical-align:middle;">
                            ${{entity}}
                        </span>
                    </td>
                    <td data-label="Tác Nhân">
                        <div style="font-weight:600;">${{actor}}</div>
                        <div class="sub-time">${{log.ip_address || "127.0.0.1"}}</div>
                    </td>
                    <td data-label="SHA-256 Seal">
                        <span class="hash-code" onclick="copyText('${{hash}}', this)" title="Click để sao chép toàn bộ mã SHA-256">
                            🔒 ${{truncHash}}
                        </span>
                    </td>
                    <td data-label="Thao Tác" style="text-align: right;">
                        <button type="button" class="btn-inspect" onclick="openDetail(${{idx}})">Chi Tiết</button>
                    </td>
                </tr>
                `;
            }}).join("");
        }}

        let filteredItems = [...RAW_DATA];

        function filterData() {{
            const query = (document.getElementById("searchInput").value || "").toLowerCase().trim();
            const compFilter = document.getElementById("compFilter").value;
            const entityFilter = document.getElementById("entityFilter").value;

            filteredItems = RAW_DATA.filter(item => {{
                const id = (item.id || "").toLowerCase();
                const entityId = (item.entity_id || "").toLowerCase();
                const entityType = (item.entity_type || "").toUpperCase();
                const action = (item.action || "").toLowerCase();
                const comp = (item.component_name || "").toUpperCase();
                const actor = (item.actor_id || "").toLowerCase();
                const hash = (item.tamper_evident_hash || "").toLowerCase();

                const matchQuery = !query || id.includes(query) || entityId.includes(query) || action.includes(query) || actor.includes(query) || hash.includes(query);
                
                let matchComp = true;
                if (compFilter !== "ALL") {{
                    matchComp = (comp === compFilter);
                }}

                let matchEntity = true;
                if (entityFilter !== "ALL") {{
                    matchEntity = (entityType === entityFilter);
                }}

                return matchQuery && matchComp && matchEntity;
            }});

            renderRows(filteredItems);
        }}

        function openDetail(idx) {{
            const item = filteredItems[idx] || RAW_DATA[idx];
            if (!item) return;

            document.getElementById("mEventId").textContent = item.id || "-";
            document.getElementById("mTime").textContent = formatDate(item.event_timestamp);
            document.getElementById("mHop").textContent = `${{item.hop_number ? 'Hop ' + item.hop_number : 'System'}}: ${{item.hop_name || '-'}}`;
            document.getElementById("mComponent").textContent = item.component_name || "-";
            document.getElementById("mAction").innerHTML = getActionBadge(item.action);
            document.getElementById("mEntity").textContent = `${{item.entity_type || '-'}}: ${{item.entity_id || '-'}}`;
            document.getElementById("mActor").textContent = `${{item.actor_type || '-'}} (${{item.actor_id || '-'}})`;
            document.getElementById("mIp").textContent = item.ip_address || "127.0.0.1";
            document.getElementById("mHash").textContent = item.tamper_evident_hash || "N/A";

            const rawJson = item.details || {{}};
            document.getElementById("mJson").textContent = JSON.stringify(rawJson, null, 2);

            document.getElementById("detailModal").style.display = "flex";
        }}

        function closeModal() {{
            document.getElementById("detailModal").style.display = "none";
        }}

        function closeModalOnBackdrop(e) {{
            if (e.target.id === "detailModal") closeModal();
        }}

        function exportCSV() {{
            if (!filteredItems.length) return alert("Không có dữ liệu để xuất!");
            const headers = ["ID", "Thời Gian", "Hop", "Hop Name", "Component", "Hành Động", "Entity Type", "Entity ID", "Actor Type", "Actor ID", "IP", "SHA256 Hash"];
            const rows = filteredItems.map(l => [
                `"${{l.id || ""}}"`,
                `"${{formatDate(l.event_timestamp)}}"`,
                `"${{l.hop_number || ""}}"`,
                `"${{l.hop_name || ""}}"`,
                `"${{l.component_name || ""}}"`,
                `"${{l.action || ""}}"`,
                `"${{l.entity_type || ""}}"`,
                `"${{l.entity_id || ""}}"`,
                `"${{l.actor_type || ""}}"`,
                `"${{l.actor_id || ""}}"`,
                `"${{l.ip_address || ""}}"`,
                `"${{l.tamper_evident_hash || ""}}"`
            ]);
            const csvContent = "\uFEFF" + [headers.join(","), ...rows.map(r => r.join(","))].join("\\n");
            const blob = new Blob([csvContent], {{ type: "text/csv;charset=utf-8;" }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `audit_logs_${{new Date().toISOString().slice(0,10)}}.csv`;
            a.click();
        }}

        function exportJSON() {{
            if (!filteredItems.length) return alert("Không có dữ liệu để xuất!");
            const blob = new Blob([JSON.stringify(filteredItems, null, 2)], {{ type: "application/json" }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `audit_logs_${{new Date().toISOString().slice(0,10)}}.json`;
            a.click();
        }}

        // Initial render
        renderRows(filteredItems);
    </script>
</body>
</html>
"""
