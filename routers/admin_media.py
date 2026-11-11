# -*- coding: utf-8 -*-
"""
Media Manager Router.
Handles visual folder/file management, WebP uploads, in-place renames, and deletions.
Decoupled from core survey ingress.
"""

import os
import re
import shutil
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from fastapi.responses import HTMLResponse
from config import settings
from core.state import verify_admin_auth, MEDIA_STORAGE_DIR
from services.normalizer import sanitize_and_normalize_image
from services.media_manager_html import render_media_manager_html

logger = logging.getLogger("routers.admin_media")
router = APIRouter(tags=["Media Management"])


def scan_media_storage_files() -> Tuple[List[Dict[str, Any]], List[str]]:
    items = []
    folders_set = set(["general", "guide", "forms"])
    if not os.path.exists(MEDIA_STORAGE_DIR):
        return items, sorted(list(folders_set))

    for root, dirs, files in os.walk(MEDIA_STORAGE_DIR):
        rel_dir = os.path.relpath(root, MEDIA_STORAGE_DIR)
        folder = "general" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        if folder != "general":
            folders_set.add(folder)
        for d in dirs:
            sub = os.path.relpath(os.path.join(root, d), MEDIA_STORAGE_DIR).replace(os.sep, "/")
            if sub != ".":
                folders_set.add(sub)
        for f in files:
            if f.startswith(".") or not f.lower().endswith((".webp", ".jpg", ".jpeg", ".png", ".gif", ".svg")):
                continue
            full_path = os.path.join(root, f)
            try:
                st = os.stat(full_path)
                size_kb = max(1, round(st.st_size / 1024))
                rel_url = f"/media/{folder}/{f}" if folder != "general" else f"/media/{f}"
                dims = ""
                try:
                    with Image.open(full_path) as im:
                        dims = f"{im.width} × {im.height}"
                except Exception:
                    dims = "WebP"
                file_id = f"{folder}/{f}" if folder != "general" else f
                items.append({
                    "id": file_id,
                    "name": f,
                    "title": os.path.splitext(f)[0].replace("_", " ").title(),
                    "folder": folder,
                    "size_kb": size_kb,
                    "dimensions": dims,
                    "url": rel_url,
                    "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.warning(f"Error scanning media file {full_path}: {e}")

    items.sort(key=lambda x: x.get("modified_at", ""), reverse=True)
    return items, sorted(list(folders_set))


def seed_media_storage():
    os.makedirs(MEDIA_STORAGE_DIR, exist_ok=True)
    for sub in ("guide", "forms", "general"):
        os.makedirs(os.path.join(MEDIA_STORAGE_DIR, sub), exist_ok=True)
    samples_dir = os.path.join(os.getcwd(), "data", "test_samples")
    if os.path.exists(samples_dir):
        guide_dir = os.path.join(MEDIA_STORAGE_DIR, "guide")
        for f in os.listdir(samples_dir):
            if f.endswith((".jpg", ".png", ".webp")) and not f.startswith("."):
                src = os.path.join(samples_dir, f)
                dst = os.path.join(guide_dir, f)
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        pass


seed_media_storage()


@router.get("/admin/media", response_class=HTMLResponse)
async def admin_media_manager_page(
    folder: str = "all",
    admin_user: str = Depends(verify_admin_auth),
):
    """Google Drive-style Media Manager Admin UI."""
    media_files, subfolders = scan_media_storage_files()
    storage_type = "Cloudflare R2" if (settings.R2_ACCOUNT_ID and settings.R2_ACCESS_KEY_ID) else "Caddy Static (/media)"
    return HTMLResponse(
        content=render_media_manager_html(
            media_files=media_files,
            folders=subfolders,
            current_folder=folder,
            environment=settings.ENVIRONMENT,
            storage_backend=storage_type,
        ),
        status_code=200,
    )


@router.get("/api/v1/media")
async def list_media_api(admin_user: str = Depends(verify_admin_auth)):
    media_files, subfolders = scan_media_storage_files()
    return {"status": "success", "files": media_files, "folders": subfolders}


@router.post("/api/v1/media/upload")
async def upload_media_api(
    file: UploadFile = File(...),
    folder: str = Form("guide"),
    admin_user: str = Depends(verify_admin_auth),
):
    clean_folder = folder.strip("/\\.").replace("\\", "/")
    if ".." in clean_folder or not clean_folder:
        clean_folder = "guide"
    target_dir = os.path.join(MEDIA_STORAGE_DIR, clean_folder)
    os.makedirs(target_dir, exist_ok=True)

    raw_bytes = await file.read()
    orig_name = os.path.splitext(file.filename or "upload")[0]
    clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", orig_name).strip("_") or "image"
    final_filename = f"{clean_name}.webp"
    final_path = os.path.join(target_dir, final_filename)

    try:
        webp_bytes, pil_im = sanitize_and_normalize_image(raw_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION)
        dims = f"{pil_im.width} × {pil_im.height}"
    except Exception:
        webp_bytes = raw_bytes
        dims = "Chuẩn WebP"

    with open(final_path, "wb") as f_out:
        f_out.write(webp_bytes)

    size_kb = max(1, round(len(webp_bytes) / 1024))
    rel_url = f"/media/{clean_folder}/{final_filename}"
    item = {
        "id": f"{clean_folder}/{final_filename}",
        "name": final_filename,
        "title": clean_name.replace("_", " ").title(),
        "folder": clean_folder,
        "size_kb": size_kb,
        "dimensions": dims,
        "url": rel_url,
        "modified_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"status": "success", "item": item}


@router.delete("/api/v1/media")
async def delete_media_api(
    id: str,
    admin_user: str = Depends(verify_admin_auth),
):
    if ".." in id:
        raise HTTPException(status_code=400, detail="Invalid media id")
    clean_id = id.strip("/\\")
    full_path = os.path.realpath(os.path.join(MEDIA_STORAGE_DIR, clean_id))
    base_path = os.path.realpath(MEDIA_STORAGE_DIR)
    if not (full_path == base_path or full_path.startswith(base_path + os.sep)):
        raise HTTPException(status_code=400, detail="Path traversal forbidden")
    if os.path.exists(full_path) and os.path.isfile(full_path):
        try:
            os.remove(full_path)
            return {"status": "success", "deleted": id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=404, detail="Media file not found")


class MediaRenamePayload(BaseModel):
    id: str
    new_name: str
    new_folder: Optional[str] = None


@router.patch("/api/v1/media/rename")
async def rename_media_api(
    payload: MediaRenamePayload,
    admin_user: str = Depends(verify_admin_auth),
):
    clean_id = payload.id.strip("/\\.")
    if ".." in clean_id:
        raise HTTPException(status_code=400, detail="Invalid media id")
    old_full_path = os.path.realpath(os.path.join(MEDIA_STORAGE_DIR, clean_id))
    base_path = os.path.realpath(MEDIA_STORAGE_DIR)
    if not (old_full_path == base_path or old_full_path.startswith(base_path + os.sep)):
        raise HTTPException(status_code=400, detail="Path traversal forbidden")
    if not (os.path.exists(old_full_path) and os.path.isfile(old_full_path)):
        raise HTTPException(status_code=404, detail="File not found")

    raw_name = payload.new_name.strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="Filename cannot be empty")
    name_root, ext = os.path.splitext(raw_name)
    clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name_root).strip("_")
    clean_ext = ext.lower() if ext.lower() in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".svg") else ".webp"
    final_name = f"{clean_name}{clean_ext}"

    if payload.new_folder is not None:
        target_folder = payload.new_folder.strip("/\\.").replace("\\", "/")
        if ".." in target_folder:
            raise HTTPException(status_code=400, detail="Invalid folder")
    else:
        target_folder = os.path.dirname(clean_id).replace("\\", "/")

    dest_dir = os.path.realpath(os.path.join(MEDIA_STORAGE_DIR, target_folder)) if target_folder and target_folder != "general" else base_path
    if not (dest_dir == base_path or dest_dir.startswith(base_path + os.sep)):
        raise HTTPException(status_code=400, detail="Path traversal forbidden")
    os.makedirs(dest_dir, exist_ok=True)
    new_full_path = os.path.join(dest_dir, final_name)

    try:
        shutil.move(old_full_path, new_full_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename/move file: {e}")

    new_folder_str = "general" if dest_dir == base_path else os.path.relpath(dest_dir, base_path).replace(os.sep, "/")
    new_id = f"{new_folder_str}/{final_name}" if new_folder_str != "general" else final_name
    rel_url = f"/media/{new_folder_str}/{final_name}" if new_folder_str != "general" else f"/media/{final_name}"

    dims = "WebP"
    try:
        with Image.open(new_full_path) as im:
            dims = f"{im.width} × {im.height}"
    except Exception:
        pass
    st = os.stat(new_full_path)
    size_kb = max(1, round(st.st_size / 1024))

    return {
        "status": "success",
        "item": {
            "id": new_id,
            "name": final_name,
            "title": clean_name.replace("_", " ").title(),
            "folder": new_folder_str,
            "size_kb": size_kb,
            "dimensions": dims,
            "url": rel_url,
            "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        }
    }
