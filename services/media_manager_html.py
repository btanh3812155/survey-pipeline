# -*- coding: utf-8 -*-
"""
Google Drive-style Media Manager Admin Frontend.
Provides visual folder/file management, drag-and-drop upload, image inspection,
and 1-click Markdown snippet copying (![alt](url)) for survey descriptions.
Decoupled from storage backend: serves directly via Caddy/FastAPI /media mount
with seamless zero-code-change cutover to Cloudflare R2.
"""

import html
import json
from typing import List, Dict, Any, Optional


def render_media_manager_html(
    media_files: List[Dict[str, Any]],
    folders: Optional[List[str]] = None,
    current_folder: str = "all",
    environment: str = "production",
    storage_backend: str = "Caddy Static (/media)",
) -> str:
    folders = folders or ["guide", "forms", "general"]
    is_dev = str(environment).lower() != "production"
    
    if is_dev:
        env_badge = '<span style="display:inline-flex; align-items:center; gap:5px; background:rgba(245,158,11,0.18); color:#f59e0b; border:1px solid rgba(245,158,11,0.45); font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:9999px; vertical-align:middle; margin-left:8px; text-transform:uppercase; letter-spacing:0.5px;">⚡ DEV REPLICA (surveydb_dev)</span>'
        env_subtext = "Môi trường Thử Nghiệm / Staging độc lập"
    else:
        env_badge = '<span style="display:inline-flex; align-items:center; gap:5px; background:rgba(16,185,129,0.15); color:#10b981; border:1px solid rgba(16,185,129,0.35); font-size:0.72rem; font-weight:700; padding:2px 8px; border-radius:9999px; vertical-align:middle; margin-left:8px; text-transform:uppercase; letter-spacing:0.5px;">✓ PRODUCTION</span>'
        env_subtext = "Hệ thống Cơ sở dữ liệu chính thức"

    files_json = json.dumps(media_files, ensure_ascii=False)
    folders_json = json.dumps(folders, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Trust Bridge - Quản Lý Media & Ảnh Biểu Mẫu</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400;1,600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #090d16;
            --surface: #131b2e;
            --surface-hover: #1a253e;
            --surface-active: #223254;
            --border: #23314f;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --accent-soft: rgba(37, 99, 235, 0.15);
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
            --card-radius: 12px;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: 'Be Vietnam Pro', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px 14px;
        }}

        .container {{
            max-width: 1500px;
            margin: 0 auto;
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 14px;
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }}

        .header-brand h1 {{
            font-size: 1.35rem;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 8px;
            color: #fff;
        }}

        .header-brand p {{
            font-size: 0.82rem;
            color: var(--muted);
            margin-top: 4px;
        }}

        .header-actions {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }}

        .nav-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            text-decoration: none;
            padding: 7px 12px;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .nav-btn:hover {{
            background: var(--surface-hover);
            border-color: #3b82f6;
            color: #60a5fa;
        }}

        .nav-btn.primary {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}

        .nav-btn.primary:hover {{
            background: var(--accent-hover);
        }}

        .nav-btn.active {{
            background: var(--accent-soft);
            border-color: var(--accent);
            color: #60a5fa;
        }}

        /* Drive Layout */
        .drive-layout {{
            display: grid;
            grid-template-columns: 240px 1fr;
            gap: 16px;
            margin-top: 14px;
        }}

        @media (max-width: 860px) {{
            .drive-layout {{
                grid-template-columns: 1fr;
            }}
        }}

        /* Sidebar Folders */
        .drive-sidebar {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--card-radius);
            padding: 14px;
            height: fit-content;
        }}

        .sidebar-section-title {{
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: var(--muted);
            font-weight: 700;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .folder-item {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 12px;
            border-radius: 8px;
            color: var(--text);
            cursor: pointer;
            font-size: 0.82rem;
            font-weight: 600;
            margin-bottom: 4px;
            transition: all 0.15s ease;
        }}

        .folder-item:hover {{
            background: var(--surface-hover);
            color: #60a5fa;
        }}

        .folder-item.active {{
            background: var(--accent-soft);
            color: #60a5fa;
            border: 1px solid rgba(37, 99, 235, 0.35);
        }}

        .folder-count {{
            margin-left: auto;
            font-size: 0.7rem;
            background: rgba(255, 255, 255, 0.08);
            padding: 2px 6px;
            border-radius: 10px;
            color: var(--muted);
        }}

        .storage-status-card {{
            margin-top: 20px;
            padding-top: 14px;
            border-top: 1px solid var(--border);
            font-size: 0.75rem;
        }}

        .storage-status-card .title {{
            font-weight: 700;
            color: var(--text);
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .storage-status-card .desc {{
            color: var(--muted);
            line-height: 1.4;
        }}

        /* Drive Main Area */
        .drive-main {{
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}

        /* Top Action / Search Bar */
        .drive-toolbar {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--card-radius);
            padding: 12px 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .search-box-wrap {{
            flex: 1;
            min-width: 240px;
            position: relative;
        }}

        .search-input {{
            width: 100%;
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 12px 8px 34px;
            color: #fff;
            font-size: 0.82rem;
            outline: none;
            transition: border-color 0.15s ease;
        }}

        .search-input:focus {{
            border-color: #3b82f6;
        }}

        .search-icon {{
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            width: 16px;
            height: 16px;
            fill: var(--muted);
            pointer-events: none;
        }}

        .toolbar-controls {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .view-toggle-btn {{
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 0.78rem;
        }}

        .view-toggle-btn.active {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}

        /* Drag & Drop Upload Zone */
        .upload-dropzone {{
            background: rgba(30, 41, 59, 0.4);
            border: 2px dashed #23314f;
            border-radius: var(--card-radius);
            padding: 24px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
        }}

        .upload-dropzone:hover, .upload-dropzone.dragover {{
            background: rgba(37, 99, 235, 0.08);
            border-color: #3b82f6;
        }}

        .upload-dropzone p {{
            font-size: 0.85rem;
            color: var(--text);
            margin-top: 8px;
            font-weight: 600;
        }}

        .upload-dropzone span {{
            font-size: 0.75rem;
            color: var(--muted);
        }}

        /* Files Grid View */
        .media-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 14px;
        }}

        .media-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--card-radius);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.15s ease, border-color 0.15s ease;
            position: relative;
        }}

        .media-card:hover {{
            transform: translateY(-2px);
            border-color: #3b82f6;
        }}

        .media-thumb-wrap {{
            width: 100%;
            height: 140px;
            background: #090d16;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            position: relative;
            cursor: pointer;
        }}

        .media-thumb {{
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            transition: transform 0.2s ease;
        }}

        .media-card:hover .media-thumb {{
            transform: scale(1.05);
        }}

        .badge-folder {{
            position: absolute;
            top: 8px;
            left: 8px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(4px);
            color: #60a5fa;
            border: 1px solid rgba(59, 130, 246, 0.3);
            font-size: 0.68rem;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            text-transform: uppercase;
        }}

        .badge-format {{
            position: absolute;
            top: 8px;
            right: 8px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(4px);
            color: #a5f3fc;
            border: 1px solid rgba(6, 182, 212, 0.3);
            font-size: 0.68rem;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            text-transform: uppercase;
        }}

        .media-info {{
            padding: 10px 12px;
            display: flex;
            flex-direction: column;
            gap: 4px;
            flex: 1;
        }}

        .media-title {{
            font-size: 0.82rem;
            font-weight: 700;
            color: #fff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .media-meta {{
            font-size: 0.7rem;
            color: var(--muted);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .media-actions {{
            display: flex;
            gap: 6px;
            margin-top: 8px;
            padding-top: 8px;
            border-top: 1px solid var(--border);
        }}

        .btn-card-action {{
            flex: 1;
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 5px 6px;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            transition: all 0.15s ease;
        }}

        .btn-card-action:hover {{
            background: #2563eb;
            color: #fff;
            border-color: #2563eb;
        }}

        .btn-card-action.danger:hover {{
            background: #ef4444;
            color: #fff;
            border-color: #ef4444;
        }}

        /* Toast notification */
        #toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: #10b981;
            color: #fff;
            padding: 10px 18px;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 700;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            display: none;
            z-index: 9999;
            animation: fadeIn 0.2s ease;
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}

        /* Preview Modal */
        .modal-overlay {{
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(5px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            padding: 20px;
        }}

        .modal-content {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            max-width: 900px;
            width: 100%;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7);
        }}

        .modal-header {{
            padding: 14px 18px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .modal-header h3 {{
            font-size: 1rem;
            font-weight: 700;
            color: #fff;
        }}

        .btn-modal-close {{
            background: transparent;
            border: none;
            color: var(--muted);
            font-size: 1.4rem;
            cursor: pointer;
            line-height: 1;
        }}

        .btn-modal-close:hover {{
            color: #fff;
        }}

        .modal-body {{
            padding: 18px;
            overflow: auto;
            display: grid;
            grid-template-columns: 1fr 320px;
            gap: 18px;
        }}

        @media (max-width: 768px) {{
            .modal-body {{
                grid-template-columns: 1fr;
            }}
        }}

        .modal-preview-img-wrap {{
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 10px;
            min-height: 250px;
        }}

        .modal-preview-img {{
            max-width: 100%;
            max-height: 380px;
            object-fit: contain;
            border-radius: 4px;
        }}

        .modal-props {{
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        .prop-item label {{
            font-size: 0.72rem;
            text-transform: uppercase;
            color: var(--muted);
            font-weight: 700;
            margin-bottom: 4px;
            display: block;
        }}

        .prop-item input, .prop-item textarea {{
            width: 100%;
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 7px 10px;
            color: #fff;
            font-size: 0.78rem;
            font-family: monospace;
            outline: none;
        }}

        .btn-copy-input {{
            margin-top: 4px;
            background: var(--surface-hover);
            border: 1px solid var(--border);
            color: #60a5fa;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.72rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}

        .btn-copy-input:hover {{
            background: #2563eb;
            color: #fff;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header>
            <div class="header-brand">
                <h1>
                    <svg viewBox="0 0 24 24" style="width:24px; height:24px; fill:#3b82f6;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zm-5.04-6.71l-2.75 3.54-1.96-2.36L6.5 17h11l-3.54-4.71z"/></svg>
                    Quản Lý Media & Ảnh Biểu Mẫu (Drive Interface)
                    {env_badge}
                </h1>
                <p>Kho lưu trữ ảnh tĩnh, ảnh minh họa câu hỏi & hỗ trợ sao chép Markdown 1-chạm ({env_subtext})</p>
            </div>
            <div class="header-actions">
                <a href="/admin/forms" class="nav-btn">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3h-4.18C14.4 1.84 13.3 1 12 1c-1.3 0-2.4.84-2.82 2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm2 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>
                    Quản Lý Form
                </a>
                <a href="/admin/submissions" class="nav-btn">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>
                    Dữ Liệu Nộp
                </a>
                <a href="/admin/audit" class="nav-btn">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/></svg>
                    Nhật Ký Kiểm Toán
                </a>
                <a href="/admin/media" class="nav-btn active">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zm-5.04-6.71l-2.75 3.54-1.96-2.36L6.5 17h11l-3.54-4.71z"/></svg>
                    Quản Lý Media
                </a>
            </div>
        </header>

        <!-- Drive Layout -->
        <div class="drive-layout">
            <!-- Left Sidebar -->
            <aside class="drive-sidebar">
                <div class="sidebar-section-title">
                    <span>Thư Mục (Folders)</span>
                    <button type="button" onclick="createNewFolder()" style="background:none; border:none; color:#60a5fa; cursor:pointer; font-size:0.75rem; font-weight:700;">+ Thêm</button>
                </div>
                <div id="foldersList">
                    <!-- Rendered by JS -->
                </div>

                <div class="storage-status-card">
                    <div class="title">
                        <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:#10b981;"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z"/></svg>
                        <span>Cơ Chế Phục Vụ</span>
                    </div>
                    <div class="desc">
                        Backend hiện tại: <b style="color:#60a5fa;">{storage_backend}</b>.<br>
                        Tự động nén WebP, lưu tại Caddy và phục vụ tức thì &lt;15ms.<br>
                        <span style="color:#f59e0b; display:inline-block; margin-top:4px;">☁ Sẵn sàng hoán chuyển sang Cloudflare R2 khi quy mô tăng trưởng.</span>
                    </div>
                </div>
            </aside>

            <!-- Main Files Area -->
            <main class="drive-main">
                <!-- Dropzone for uploads -->
                <div class="upload-dropzone" id="dropzone" onclick="document.getElementById('fileInput').click()">
                    <input type="file" id="fileInput" accept="image/*" multiple style="display:none;" onchange="handleFileSelect(event)">
                    <svg viewBox="0 0 24 24" style="width:36px; height:36px; fill:#3b82f6;"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/></svg>
                    <p>Kéo thả ảnh vào đây hoặc nhấp để tải lên</p>
                    <span>Tự động tối ưu hóa sang WebP chuẩn, chống méo tỉ lệ & strip EXIF an toàn</span>
                </div>

                <!-- Toolbar -->
                <div class="drive-toolbar">
                    <div class="search-box-wrap">
                        <svg class="search-icon" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
                        <input type="text" id="searchInput" class="search-input" placeholder="Tìm kiếm ảnh theo tên file, định dạng hoặc kích thước..." oninput="filterMedia()">
                    </div>
                    <div class="toolbar-controls">
                        <span id="filesCountBadge" style="font-size:0.75rem; color:var(--muted); margin-right:8px;">0 tệp</span>
                        <button type="button" class="view-toggle-btn active" id="btnGrid" onclick="setViewMode('grid')">⊞ Lưới</button>
                        <button type="button" class="view-toggle-btn" id="btnList" onclick="setViewMode('list')">☰ Danh sách</button>
                    </div>
                </div>

                <!-- Media Grid Container -->
                <div class="media-grid" id="mediaGrid">
                    <!-- Rendered by JS -->
                </div>
            </main>
        </div>
    </div>

    <!-- Toast Notification -->
    <div id="toast">Đã sao chép vào bộ nhớ tạm!</div>

    <!-- Preview Modal -->
    <div class="modal-overlay" id="previewModal" onclick="closePreviewModal(event)">
        <div class="modal-content" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h3 id="modalFileTitle">Chi Tiết Tệp Media</h3>
                <button type="button" class="btn-modal-close" onclick="closePreviewModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="modal-preview-img-wrap">
                    <img id="modalPreviewImg" class="modal-preview-img" src="" alt="Preview">
                </div>
                <div class="modal-props">
                    <div class="prop-item">
                        <label>Tên Tệp (Filename)</label>
                        <input type="text" id="propFilename" readonly>
                    </div>
                    <div class="prop-item">
                        <label>Thư Mục (Folder)</label>
                        <input type="text" id="propFolder" readonly>
                    </div>
                    <div class="prop-item">
                        <label>Độ Phân Giải & Dung Lượng</label>
                        <input type="text" id="propResolution" readonly>
                    </div>
                    <div class="prop-item">
                        <label>Đường Dẫn URL Trực Tiếp</label>
                        <input type="text" id="propUrl" readonly>
                        <button type="button" class="btn-copy-input" onclick="copyModalField('propUrl')">🔗 Sao chép URL</button>
                    </div>
                    <div class="prop-item">
                        <label>Cú Pháp Markdown Mô Tả Form</label>
                        <textarea id="propMarkdown" rows="2" readonly></textarea>
                        <button type="button" class="btn-copy-input" onclick="copyModalField('propMarkdown')">📋 Sao chép Markdown</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const RAW_MEDIA = {files_json};
        const ALL_FOLDERS = {folders_json};
        let currentFolder = "{current_folder}";
        let currentView = "grid";
        let filteredMedia = [...RAW_MEDIA];

        function init() {{
            renderFolders();
            filterMedia();
            setupDropzone();
        }}

        function showToast(msg) {{
            const t = document.getElementById("toast");
            t.textContent = msg;
            t.style.display = "block";
            setTimeout(() => {{ t.style.display = "none"; }}, 2500);
        }}

        function copyToClipboard(text, successMsg = "Đã sao chép vào bộ nhớ tạm!") {{
            navigator.clipboard.writeText(text).then(() => {{
                showToast(successMsg);
            }}).catch(err => {{
                prompt("Sao chép liên kết thủ công:", text);
            }});
        }}

        function renderFolders() {{
            const list = document.getElementById("foldersList");
            const counts = {{ "all": RAW_MEDIA.length }};
            ALL_FOLDERS.forEach(f => {{
                counts[f] = RAW_MEDIA.filter(m => m.folder === f).length;
            }});

            let html = `
                <div class="folder-item ${{currentFolder === 'all' ? 'active' : ''}}" onclick="selectFolder('all')">
                    <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14z"/></svg>
                    <span>Tất Cả Tệp</span>
                    <span class="folder-count">${{counts['all']}}</span>
                </div>
            `;

            ALL_FOLDERS.forEach(f => {{
                const label = f === 'guide' ? 'Hướng Dẫn (guide)' : (f === 'forms' ? 'Biểu Mẫu (forms)' : (f === 'general' ? 'Chung (general)' : f));
                html += `
                    <div class="folder-item ${{currentFolder === f ? 'active' : ''}}" onclick="selectFolder('${{f}}')">
                        <svg viewBox="0 0 24 24" style="width:16px; height:16px; fill:currentColor;"><path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
                        <span>${{label}}</span>
                        <span class="folder-count">${{counts[f] || 0}}</span>
                    </div>
                `;
            }});

            list.innerHTML = html;
        }}

        function selectFolder(folder) {{
            currentFolder = folder;
            renderFolders();
            filterMedia();
        }}

        function filterMedia() {{
            const q = (document.getElementById("searchInput").value || "").toLowerCase().trim();
            filteredMedia = RAW_MEDIA.filter(m => {{
                const matchFolder = (currentFolder === 'all') || (m.folder === currentFolder);
                const matchQuery = !q || m.name.toLowerCase().includes(q) || (m.folder && m.folder.toLowerCase().includes(q));
                return matchFolder && matchQuery;
            }});
            renderMediaGrid();
        }}

        function renderMediaGrid() {{
            const grid = document.getElementById("mediaGrid");
            document.getElementById("filesCountBadge").textContent = `${{filteredMedia.length}} tệp`;

            if (filteredMedia.length === 0) {{
                grid.innerHTML = `
                    <div style="grid-column: 1/-1; padding: 40px; text-align: center; color: var(--muted); background: var(--surface); border: 1px dashed var(--border); border-radius: 12px;">
                        <svg viewBox="0 0 24 24" style="width:40px; height:40px; fill:currentColor; margin-bottom:8px;"><path d="M19 5v14H5V5h14m0-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-4.86 8.86l-3 3.87L9 13.14 6 17h12l-3.86-5.14z"/></svg>
                        <p style="font-weight:600;">Không có tệp media nào trong thư mục này</p>
                        <span style="font-size:0.75rem;">Kéo thả ảnh lên khu vực tải lên ở trên để thêm tệp mới</span>
                    </div>
                `;
                return;
            }}

            let html = "";
            filteredMedia.forEach(m => {{
                const fullUrl = window.location.origin + m.url;
                const mdSnippet = `![${{m.title || m.name}}](${{fullUrl}})`;
                const ext = m.name.split('.').pop().toUpperCase();
                const res = m.dimensions || "Chuẩn WebP";

                html += `
                    <div class="media-card">
                        <div class="media-thumb-wrap" onclick="openPreviewModal('${{m.id}}')">
                            <span class="badge-folder">${{m.folder}}</span>
                            <span class="badge-format">${{ext}}</span>
                            <img src="${{m.url}}" alt="${{m.name}}" class="media-thumb" loading="lazy">
                        </div>
                        <div class="media-info">
                            <div class="media-title" title="${{m.name}}">${{m.title || m.name}}</div>
                            <div class="media-meta">
                                <span>${{res}}</span>
                                <span>${{m.size_kb}} KB</span>
                            </div>
                            <div class="media-actions">
                                <button type="button" class="btn-card-action" onclick="copyToClipboard('${{mdSnippet.replace(/'/g, "\\\\'") }}', 'Đã sao chép Markdown mô tả form!')" title="Sao chép cú pháp Markdown">
                                    📋 Markdown
                                </button>
                                <button type="button" class="btn-card-action" onclick="copyToClipboard('${{fullUrl}}', 'Đã sao chép link ảnh trực tiếp!')" title="Sao chép Link URL">
                                    🔗 Link
                                </button>
                                <button type="button" class="btn-card-action danger" onclick="deleteMedia('${{m.id}}', '${{m.name}}')" title="Xóa tệp">
                                    🗑
                                </button>
                            </div>
                        </div>
                    </div>
                `;
            }});

            grid.innerHTML = html;
        }}

        function openPreviewModal(id) {{
            const item = RAW_MEDIA.find(m => m.id === id);
            if (!item) return;

            const fullUrl = window.location.origin + item.url;
            const mdSnippet = `![${{item.title || item.name}}](${{fullUrl}})`;

            document.getElementById("modalFileTitle").textContent = item.title || item.name;
            document.getElementById("modalPreviewImg").src = item.url;
            document.getElementById("propFilename").value = item.name;
            document.getElementById("propFolder").value = item.folder || "general";
            document.getElementById("propResolution").value = `${{item.dimensions || 'N/A'}} (${{item.size_kb}} KB)`;
            document.getElementById("propUrl").value = fullUrl;
            document.getElementById("propMarkdown").value = mdSnippet;

            document.getElementById("previewModal").style.display = "flex";
        }}

        function closePreviewModal(e) {{
            if (!e || e.target.id === "previewModal" || e.target.classList.contains("btn-modal-close")) {{
                document.getElementById("previewModal").style.display = "none";
            }}
        }}

        function copyModalField(elementId) {{
            const el = document.getElementById(elementId);
            if (el) {{
                copyToClipboard(el.value, "Đã sao chép thông tin!");
            }}
        }}

        function setViewMode(mode) {{
            currentView = mode;
            document.getElementById("btnGrid").classList.toggle("active", mode === "grid");
            document.getElementById("btnList").classList.toggle("active", mode === "list");
            const grid = document.getElementById("mediaGrid");
            if (mode === "list") {{
                grid.style.display = "flex";
                grid.style.flexDirection = "column";
            }} else {{
                grid.style.display = "grid";
                grid.style.flexDirection = "initial";
            }}
        }}

        function setupDropzone() {{
            const dz = document.getElementById("dropzone");
            ['dragenter', 'dragover'].forEach(eventName => {{
                dz.addEventListener(eventName, (e) => {{
                    e.preventDefault();
                    e.stopPropagation();
                    dz.classList.add('dragover');
                }}, false);
            }});

            ['dragleave', 'drop'].forEach(eventName => {{
                dz.addEventListener(eventName, (e) => {{
                    e.preventDefault();
                    e.stopPropagation();
                    dz.classList.remove('dragover');
                }}, false);
            }});

            dz.addEventListener('drop', (e) => {{
                const dt = e.dataTransfer;
                const files = dt.files;
                handleFilesUpload(files);
            }}, false);
        }}

        function handleFileSelect(e) {{
            handleFilesUpload(e.target.files);
        }}

        async function handleFilesUpload(files) {{
            if (!files || files.length === 0) return;
            const targetFolder = (currentFolder === 'all') ? 'guide' : currentFolder;

            for (let i = 0; i < files.length; i++) {{
                const file = files[i];
                const formData = new FormData();
                formData.append("file", file);
                formData.append("folder", targetFolder);

                try {{
                    showToast(`Đang nén & tải lên ${{file.name}}...`);
                    const resp = await fetch("/api/v1/media/upload", {{
                        method: "POST",
                        body: formData
                    }});
                    const data = await resp.json();
                    if (resp.ok && data.status === "success") {{
                        RAW_MEDIA.unshift(data.item);
                        showToast(`Đã tải lên ${{file.name}} thành công!`);
                    }} else {{
                        alert("Lỗi tải lên: " + (data.detail || JSON.stringify(data)));
                    }}
                }} catch (err) {{
                    alert("Lỗi mạng tải lên: " + err);
                }}
            }}

            renderFolders();
            filterMedia();
        }}

        async function deleteMedia(id, filename) {{
            if (!confirm(`Bạn có chắc muốn xóa tệp '${{filename}}'? Thao tác này không thể hoàn tác.`)) {{
                return;
            }}

            try {{
                const resp = await fetch(`/api/v1/media?id=${{encodeURIComponent(id)}}`, {{
                    method: "DELETE"
                }});
                const res = await resp.json();
                if (resp.ok && res.status === "success") {{
                    const idx = RAW_MEDIA.findIndex(m => m.id === id);
                    if (idx !== -1) RAW_MEDIA.splice(idx, 1);
                    showToast(`Đã xóa tệp ${{filename}}!`);
                    renderFolders();
                    filterMedia();
                }} else {{
                    alert("Lỗi xóa: " + (res.detail || JSON.stringify(res)));
                }}
            }} catch (err) {{
                alert("Lỗi mạng khi xóa: " + err);
            }}
        }}

        function createNewFolder() {{
            const name = prompt("Nhập tên thư mục mới (không dấu, viết thường, ví dụ: banners, tutorials):");
            if (name && name.trim()) {{
                const clean = name.trim().toLowerCase().replace(/[^a-z0-9_-]/g, "");
                if (clean && !ALL_FOLDERS.includes(clean)) {{
                    ALL_FOLDERS.push(clean);
                    selectFolder(clean);
                }}
            }}
        }}

        document.addEventListener("DOMContentLoaded", init);
    </script>
</body>
</html>
"""
