# -*- coding: utf-8 -*-
"""
HTML rendering service for the Survey Submissions Viewer Dashboard.
Provides a modern, responsive web interface for inspecting survey submissions
from both PostgreSQL master storage and dev test storage.
"""

import json
from typing import List, Dict, Any, Optional


def render_submissions_viewer_html(
    submissions: List[Dict[str, Any]],
    stats: Optional[Dict[str, int]] = None,
    current_mode: str = "all"
) -> str:
    stats = stats or {
        "total": len(submissions),
        "approved": sum(1 for s in submissions if s.get("status") == "APPROVED"),
        "rejected": sum(1 for s in submissions if s.get("status") == "REJECTED"),
        "manual_review": sum(1 for s in submissions if s.get("status") in ("MANUAL_REVIEW", "APPEAL_PENDING", "PENDING")),
    }

    # Pre-encode submissions for frontend filtering and modal viewing
    submissions_json = json.dumps(submissions, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Trust Bridge - Dữ Liệu Khảo Sát Đã Nộp</title>
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

        .container {{ max-width: 1280px; margin: 0 auto; }}

        /* Top Navigation Header */
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

        /* KPI Cards */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
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
        .stat-card.approved::before {{ background: var(--success); }}
        .stat-card.rejected::before {{ background: var(--danger); }}
        .stat-card.review::before {{ background: var(--warning); }}

        .stat-title {{
            font-size: 0.82rem;
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
            min-width: 240px;
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
            font-size: 0.86rem;
        }}

        th {{
            background: rgba(35, 49, 79, 0.4);
            padding: 12px 16px;
            font-weight: 700;
            color: var(--muted);
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }}

        td {{
            padding: 14px 16px;
            border-bottom: 1px solid var(--border);
            vertical-align: middle;
        }}

        tr:hover td {{
            background: var(--surface-hover);
        }}

        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }}

        .badge.approved {{ background: var(--success-bg); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge.rejected {{ background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }}
        .badge.review {{ background: var(--warning-bg); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge.test {{ background: var(--info-bg); color: var(--info); border: 1px solid rgba(6, 182, 212, 0.3); }}
        .badge.prod {{ background: rgba(148, 163, 184, 0.15); color: var(--muted); border: 1px solid var(--border); }}

        .sub-id {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-weight: 700;
            color: #60a5fa;
        }}

        .sub-time {{
            font-size: 0.75rem;
            color: var(--muted);
            margin-top: 3px;
        }}

        .driver-name {{ font-weight: 600; }}
        .driver-phone {{ font-size: 0.78rem; color: var(--muted); }}

        .btn-detail {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text);
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.8rem;
            font-weight: 600;
            transition: all 0.15s;
        }}

        .btn-detail:hover {{
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

        /* Detail Modal */
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
            max-width: 800px;
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
            font-size: 0.9rem;
            font-weight: 600;
        }}

        .json-preview {{
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.78rem;
            max-height: 250px;
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
                font-size: 0.78rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-brand">
                <h1>📊 Dữ Liệu Khảo Sát Đã Nộp</h1>
                <p>Trust Bridge Survey Verification & Fraud Detection Data Hub</p>
            </div>
            <div class="header-actions">
                <a href="/admin/forms" class="nav-btn">📋 Quản Lý Form</a>
                <a href="/admin/samples" class="nav-btn">🖼️ Ảnh Mẫu Test</a>
                <button type="button" onclick="location.reload()" class="nav-btn primary">🔄 Làm Mới</button>
            </div>
        </header>

        <!-- KPI Summary Cards -->
        <div class="stats-grid">
            <div class="stat-card total">
                <div class="stat-title">Tổng Số Hồ Sơ</div>
                <div class="stat-value" id="kpiTotal">{stats["total"]}</div>
            </div>
            <div class="stat-card approved">
                <div class="stat-title">Đã Duyệt (Approved)</div>
                <div class="stat-value" id="kpiApproved" style="color: var(--success);">{stats["approved"]}</div>
            </div>
            <div class="stat-card review">
                <div class="stat-title">Chờ Duyệt (Manual Review)</div>
                <div class="stat-value" id="kpiReview" style="color: var(--warning);">{stats["manual_review"]}</div>
            </div>
            <div class="stat-card rejected">
                <div class="stat-title">Bị Từ Chối (Rejected)</div>
                <div class="stat-value" id="kpiRejected" style="color: var(--danger);">{stats["rejected"]}</div>
            </div>
        </div>

        <!-- Filter & Search Toolbar -->
        <div class="controls-bar">
            <div class="filter-group">
                <input type="text" id="searchInput" class="search-box" placeholder="🔍 Tìm theo ID, SĐT, Tên tài xế, Biển số..." oninput="filterData()">
                <select id="statusFilter" class="select-filter" onchange="filterData()">
                    <option value="ALL">Tất cả trạng thái</option>
                    <option value="APPROVED">Đã Duyệt (APPROVED)</option>
                    <option value="REJECTED">Bị Từ Chối (REJECTED)</option>
                    <option value="MANUAL_REVIEW">Chờ Duyệt (MANUAL_REVIEW)</option>
                </select>
                <select id="sourceFilter" class="select-filter" onchange="filterData()">
                    <option value="ALL">Tất cả nguồn lưu trữ</option>
                    <option value="PROD">Chính thức (Postgres)</option>
                    <option value="TEST_DEV_MODE">Thử nghiệm (Dev Test)</option>
                </select>
            </div>
            <div class="export-group">
                <button type="button" class="nav-btn" onclick="exportCSV()">📥 Xuất CSV</button>
                <button type="button" class="nav-btn" onclick="exportJSON()">📥 Xuất JSON</button>
            </div>
        </div>

        <!-- Submissions Table -->
        <div class="table-wrap">
            <table id="submissionsTable">
                <thead>
                    <tr>
                        <th>Mã Hồ Sơ</th>
                        <th>Tài Xế / Người Nộp</th>
                        <th>Phương Tiện</th>
                        <th>Trạng Thái</th>
                        <th>Lý Do / Ghi Chú</th>
                        <th>Nguồn Lưu</th>
                        <th style="text-align: right;">Thao Tác</th>
                    </tr>
                </thead>
                <tbody id="tableBody">
                    <!-- Populated dynamically by JS -->
                </tbody>
            </table>
            <div id="emptyState" class="empty-state" style="display: none;">
                <svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>
                <h3>Chưa có dữ liệu khảo sát nào phù hợp bộ lọc</h3>
                <p>Dữ liệu sẽ xuất hiện tự động ngay khi tài xế nộp khảo sát.</p>
            </div>
        </div>
    </div>

    <!-- Details Modal -->
    <div id="detailModal" class="modal-overlay" onclick="closeModalOnBackdrop(event)">
        <div class="modal-box">
            <div class="modal-header">
                <h3 id="modalTitle">Chi Tiết Hồ Sơ</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="meta-grid">
                    <div class="meta-item">
                        <label>Mã Hồ Sơ</label>
                        <span id="mId" class="sub-id">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Thời Gian Nộp</label>
                        <span id="mTime">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Trạng Thái</label>
                        <span id="mStatus">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Tài Xế</label>
                        <span id="mDriver">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Số Điện Thoại</label>
                        <span id="mPhone">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Biển Số Xe</label>
                        <span id="mPlate">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Vị Trí GPS</label>
                        <span id="mGps">-</span>
                    </div>
                    <div class="meta-item">
                        <label>Nguồn Lưu Trữ</label>
                        <span id="mSource">-</span>
                    </div>
                </div>

                <div id="reasonBox" style="display:none; background:var(--danger-bg); border:1px solid rgba(239,68,68,0.3); border-radius:8px; padding:12px; color:#fca5a5;">
                    <b style="color:var(--danger);">Lý do từ chối:</b> <span id="mReason"></span>
                </div>

                <div>
                    <label style="display:block; font-size:0.75rem; color:var(--muted); text-transform:uppercase; font-weight:700; margin-bottom:6px;">Dữ Liệu Trích Xuất AI / Chi Tiết JSON</label>
                    <pre id="mJson" class="json-preview"></pre>
                </div>
            </div>
        </div>
    </div>

    <script>
        const RAW_DATA = {submissions_json};

        function formatDate(dStr) {{
            if (!dStr) return "-";
            try {{
                const d = new Date(dStr);
                return d.toLocaleString("vi-VN", {{ hour12: false }});
            }} catch(e) {{
                return dStr;
            }}
        }}

        function getStatusBadge(status) {{
            const s = (status || "").toUpperCase();
            if (s === "APPROVED") return '<span class="badge approved">Đã Duyệt</span>';
            if (s === "REJECTED") return '<span class="badge rejected">Từ Chối</span>';
            if (s === "MANUAL_REVIEW" || s === "APPEAL_PENDING") return '<span class="badge review">Chờ Duyệt</span>';
            return `<span class="badge prod">${{s || "N/A"}}</span>`;
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

            tbody.innerHTML = items.map((sub, idx) => {{
                const id = sub.submission_id || "-";
                const time = formatDate(sub.created_at || sub.submitted_at);
                const name = sub.driver_name || "Tài xế";
                const phone = sub.driver_phone || "Không xác định";
                const plate = sub.license_plate || sub.receipt_serial_no || "-";
                const vType = sub.vehicle_type || "";
                const statusBadge = getStatusBadge(sub.status);
                const reason = sub.rejection_reason || sub.reason || (sub.status === "APPROVED" ? "Hợp lệ" : "-");
                const isDev = sub.mode === "TEST_DEV_MODE" || id.startsWith("DEV-");
                const sourceBadge = isDev ? '<span class="badge test">Dev Test</span>' : '<span class="badge prod">Postgres</span>';

                return `
                <tr>
                    <td data-label="Mã Hồ Sơ">
                        <div class="sub-id">${{id}}</div>
                        <div class="sub-time">${{time}}</div>
                    </td>
                    <td data-label="Tài Xế">
                        <div class="driver-name">${{name}}</div>
                        <div class="driver-phone">${{phone}}</div>
                    </td>
                    <td data-label="Phương Tiện">
                        <div style="font-weight:600;">${{plate}}</div>
                        <div style="font-size:0.75rem; color:var(--muted);">${{vType}}</div>
                    </td>
                    <td data-label="Trạng Thái">${{statusBadge}}</td>
                    <td data-label="Ghi Chú" style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                        ${{reason}}
                    </td>
                    <td data-label="Nguồn Lưu">${{sourceBadge}}</td>
                    <td data-label="Thao Tác" style="text-align: right;">
                        <button type="button" class="btn-detail" onclick="openDetail(${{idx}})">Xem Chi Tiết</button>
                    </td>
                </tr>
                `;
            }}).join("");
        }}

        let filteredItems = [...RAW_DATA];

        function filterData() {{
            const query = (document.getElementById("searchInput").value || "").toLowerCase().trim();
            const statusFilter = document.getElementById("statusFilter").value;
            const sourceFilter = document.getElementById("sourceFilter").value;

            filteredItems = RAW_DATA.filter(item => {{
                const id = (item.submission_id || "").toLowerCase();
                const name = (item.driver_name || "").toLowerCase();
                const phone = (item.driver_phone || "").toLowerCase();
                const plate = (item.license_plate || item.receipt_serial_no || "").toLowerCase();
                const reason = (item.rejection_reason || item.reason || "").toLowerCase();

                const matchQuery = !query || id.includes(query) || name.includes(query) || phone.includes(query) || plate.includes(query) || reason.includes(query);
                
                const itemStatus = (item.status || "").toUpperCase();
                let matchStatus = true;
                if (statusFilter !== "ALL") {{
                    if (statusFilter === "MANUAL_REVIEW") {{
                        matchStatus = (itemStatus === "MANUAL_REVIEW" || itemStatus === "APPEAL_PENDING");
                    }} else {{
                        matchStatus = (itemStatus === statusFilter);
                    }}
                }}

                const isDev = item.mode === "TEST_DEV_MODE" || (item.submission_id || "").startsWith("DEV-");
                let matchSource = true;
                if (sourceFilter === "TEST_DEV_MODE") matchSource = isDev;
                if (sourceFilter === "PROD") matchSource = !isDev;

                return matchQuery && matchStatus && matchSource;
            }});

            renderRows(filteredItems);
        }}

        function openDetail(idx) {{
            const item = filteredItems[idx] || RAW_DATA[idx];
            if (!item) return;

            document.getElementById("mId").textContent = item.submission_id || "-";
            document.getElementById("mTime").textContent = formatDate(item.created_at || item.submitted_at);
            document.getElementById("mStatus").innerHTML = getStatusBadge(item.status);
            document.getElementById("mDriver").textContent = item.driver_name || "Tài xế";
            document.getElementById("mPhone").textContent = item.driver_phone || "-";
            document.getElementById("mPlate").textContent = item.license_plate || item.receipt_serial_no || "-";
            
            if (item.gps_latitude && item.gps_longitude) {{
                const gUrl = `https://www.google.com/maps?q=${{item.gps_latitude}},${{item.gps_longitude}}`;
                document.getElementById("mGps").innerHTML = `<a href="${{gUrl}}" target="_blank" style="color:#60a5fa; text-decoration:underline;">${{item.gps_latitude.toFixed(4)}}, ${{item.gps_longitude.toFixed(4)}} (Bản đồ)</a>`;
            }} else {{
                document.getElementById("mGps").textContent = "Không có";
            }}

            const isDev = item.mode === "TEST_DEV_MODE" || (item.submission_id || "").startsWith("DEV-");
            document.getElementById("mSource").innerHTML = isDev ? '<span class="badge test">Dev Test Storage</span>' : '<span class="badge prod">PostgreSQL SSOT</span>';

            const reason = item.rejection_reason || item.reason;
            const rBox = document.getElementById("reasonBox");
            if (item.status === "REJECTED" && reason) {{
                rBox.style.display = "block";
                document.getElementById("mReason").textContent = reason;
            }} else {{
                rBox.style.display = "none";
            }}

            const rawJson = item.ai_result || item.raw_extracted_facts || item;
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
            const headers = ["Mã Hồ Sơ", "Thời Gian", "Tài Xế", "SĐT", "Biển Số", "Trạng Thái", "Lý Do", "Nguồn"];
            const rows = filteredItems.map(s => [
                `"${{s.submission_id || ""}}"`,
                `"${{formatDate(s.created_at || s.submitted_at)}}"`,
                `"${{s.driver_name || ""}}"`,
                `"${{s.driver_phone || ""}}"`,
                `"${{s.license_plate || s.receipt_serial_no || ""}}"`,
                `"${{s.status || ""}}"`,
                `"${{(s.rejection_reason || s.reason || "").replace(/"/g, '""')}}"`,
                `"${{s.mode === "TEST_DEV_MODE" ? "Dev Test" : "Postgres"}}"`
            ]);
            const csvContent = "\uFEFF" + [headers.join(","), ...rows.map(r => r.join(","))].join("\\n");
            const blob = new Blob([csvContent], {{ type: "text/csv;charset=utf-8;" }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `submissions_${{new Date().toISOString().slice(0,10)}}.csv`;
            a.click();
        }}

        function exportJSON() {{
            if (!filteredItems.length) return alert("Không có dữ liệu để xuất!");
            const blob = new Blob([JSON.stringify(filteredItems, null, 2)], {{ type: "application/json" }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `submissions_${{new Date().toISOString().slice(0,10)}}.json`;
            a.click();
        }}

        // Initial render
        renderRows(filteredItems);
    </script>
</body>
</html>
"""
