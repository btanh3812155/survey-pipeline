"""Best-effort Google Drive cleanup for deleted forms, deleted submissions and stale image versions.

Drive is a mirror, never the source of truth. Every function here is safe to skip
when credentials or the root folder are unavailable: the database and local
private media remain authoritative.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import httpx

from services.google_drive_delivery import GoogleDriveClient

logger = logging.getLogger("services.drive_cleanup")

FOLDER_MIME = "application/vnd.google-apps.folder"


async def _find_folder(
    drive: GoogleDriveClient, http: httpx.AsyncClient, token: str, parent_id: str, name: str
) -> Optional[str]:
    return await drive.find_file_or_folder(http, token, name, parent_id, FOLDER_MIME)


async def _delete_tree(
    drive: GoogleDriveClient, http: httpx.AsyncClient, token: str, folder_id: str
) -> int:
    """Delete a folder and every non-trashed descendant. Returns deleted item count."""
    deleted = 0
    for child in await drive.list_children(http, token, folder_id):
        child_id = child.get("id")
        if not child_id:
            continue
        if child.get("mimeType") == FOLDER_MIME:
            deleted += await _delete_tree(drive, http, token, child_id)
        if await drive.delete_file(http, token, child_id):
            deleted += 1
    if await drive.delete_file(http, token, folder_id):
        deleted += 1
    return deleted


async def _delete_named_folder(
    drive: GoogleDriveClient, http: httpx.AsyncClient, token: str, parent_id: str, name: str
) -> int:
    folder_id = await _find_folder(drive, http, token, parent_id, name)
    return await _delete_tree(drive, http, token, folder_id) if folder_id else 0


async def _tier_folders(
    drive: GoogleDriveClient, http: httpx.AsyncClient, token: str, root_id: str, settings: dict[str, Any]
) -> list[str]:
    master_id = await _find_folder(drive, http, token, root_id, settings["master_folder_name"])
    if not master_id:
        return []
    folders: list[str] = []
    for key in ("client_folder_name", "internal_folder_name"):
        tier_id = await _find_folder(drive, http, token, master_id, settings[key])
        if not tier_id:
            continue
        for child in await drive.list_children(http, token, tier_id):
            if child.get("mimeType") == FOLDER_MIME and child.get("id"):
                folders.append(child["id"])
    return folders


async def _delete_submission_from_form_folder(
    drive: GoogleDriveClient, http: httpx.AsyncClient, token: str, form_folder_id: str, submission_id: str
) -> int:
    deleted = 0
    images_id = await _find_folder(drive, http, token, form_folder_id, "Images")
    if images_id:
        deleted += await _delete_named_folder(drive, http, token, images_id, submission_id)
    # Older layouts put submission folders directly under the form folder.
    deleted += await _delete_named_folder(drive, http, token, form_folder_id, submission_id)
    return deleted


async def _legacy_submission_cleanup(
    drive: GoogleDriveClient, http: httpx.AsyncClient, token: str, root_id: str,
    form_id: str, submission_id: str,
) -> int:
    deleted = 0
    legacy_images = await _find_folder(drive, http, token, root_id, "Images")
    if legacy_images:
        form_folder = await _find_folder(drive, http, token, legacy_images, form_id)
        if form_folder:
            submissions = await _find_folder(drive, http, token, form_folder, "submissions")
            if submissions:
                deleted += await _delete_named_folder(drive, http, token, submissions, submission_id)
    for parent_name in (form_id,):
        form_folder = await _find_folder(drive, http, token, root_id, parent_name)
        if form_folder:
            submissions = await _find_folder(drive, http, token, form_folder, "submissions")
            if submissions:
                deleted += await _delete_named_folder(drive, http, token, submissions, submission_id)
    internal = await _find_folder(drive, http, token, root_id, "Internal")
    if internal:
        form_folder = await _find_folder(drive, http, token, internal, form_id)
        if form_folder:
            submissions = await _find_folder(drive, http, token, form_folder, "submissions")
            if submissions:
                for status_folder in await drive.list_children(http, token, submissions):
                    if status_folder.get("mimeType") == FOLDER_MIME and status_folder.get("id"):
                        deleted += await _delete_named_folder(
                            drive, http, token, status_folder["id"], submission_id
                        )
    return deleted


async def purge_form_folders(
    drive: GoogleDriveClient,
    http: httpx.AsyncClient,
    token: str,
    root_id: str,
    settings: dict[str, Any],
    form_id: str,
) -> int:
    """Delete form folders under the current layout and legacy mirrors."""
    deleted = 0
    for tier_id in await _tier_folders(drive, http, token, root_id, settings):
        deleted += await _delete_named_folder(drive, http, token, tier_id, form_id)
    master_id = await _find_folder(drive, http, token, root_id, settings["master_folder_name"])
    if master_id:
        images_id = await _find_folder(drive, http, token, master_id, settings["images_folder_name"])
        if images_id:
            deleted += await _delete_named_folder(drive, http, token, images_id, form_id)
    # Remove legacy root-level mirrors as well.
    deleted += await _delete_named_folder(drive, http, token, root_id, form_id)
    internal = await _find_folder(drive, http, token, root_id, "Internal")
    if internal:
        deleted += await _delete_named_folder(drive, http, token, internal, form_id)
    return deleted


async def cleanup_form_drive(form_id: str, pool=None) -> dict[str, Any]:
    """Remove every Drive folder tied to a deleted form ID."""
    from core.drive_exports import DEFAULT_SETTINGS, get_settings
    drive = GoogleDriveClient()
    http = httpx.AsyncClient(timeout=60.0)
    try:
        token = await drive.get_token(http)
        if not token or not drive.root_folder_id:
            return {"status": "unavailable", "deleted": 0}
        settings = await get_settings(pool) if pool else dict(DEFAULT_SETTINGS)
        deleted = await purge_form_folders(
            drive, http, token, drive.root_folder_id, settings, form_id
        )
        logger.info("Drive cleanup removed %s items for deleted form %s", deleted, form_id)
        return {"status": "ok", "deleted": deleted}
    except Exception as exc:
        logger.warning("Drive cleanup failed for form %s: %s", form_id, type(exc).__name__)
        return {"status": "error", "deleted": 0, "error": type(exc).__name__}
    finally:
        await http.aclose()


async def cleanup_submission_drive(form_id: str, submission_id: str, pool=None) -> dict[str, Any]:
    """Remove the master images and every client/internal shortcut folder for one submission."""
    from core.drive_exports import DEFAULT_SETTINGS, get_settings
    drive = GoogleDriveClient()
    http = httpx.AsyncClient(timeout=60.0)
    try:
        token = await drive.get_token(http)
        if not token or not drive.root_folder_id:
            return {"status": "unavailable", "deleted": 0}
        settings = await get_settings(pool) if pool else dict(DEFAULT_SETTINGS)
        root_id = drive.root_folder_id
        deleted = 0
        master_id = await _find_folder(drive, http, token, root_id, settings["master_folder_name"])
        if master_id:
            images_id = await _find_folder(drive, http, token, master_id, settings["images_folder_name"])
            if images_id:
                form_folder = await _find_folder(drive, http, token, images_id, form_id)
                if form_folder:
                    deleted += await _delete_named_folder(
                        drive, http, token, form_folder, submission_id
                    )
        for tier_id in await _tier_folders(drive, http, token, root_id, settings):
            form_folder = await _find_folder(drive, http, token, tier_id, form_id)
            if form_folder:
                deleted += await _delete_submission_from_form_folder(
                    drive, http, token, form_folder, submission_id
                )
        deleted += await _legacy_submission_cleanup(
            drive, http, token, root_id, form_id, submission_id
        )
        logger.info(
            "Drive cleanup removed %s items for deleted submission %s/%s",
            deleted, form_id, submission_id,
        )
        return {"status": "ok", "deleted": deleted}
    except Exception as exc:
        logger.warning(
            "Drive cleanup failed for submission %s/%s: %s",
            form_id, submission_id, type(exc).__name__,
        )
        return {"status": "error", "deleted": 0, "error": type(exc).__name__}
    finally:
        await http.aclose()


async def prune_stale_files(
    drive: GoogleDriveClient,
    http: httpx.AsyncClient,
    token: str,
    folder_id: str,
    expected_names: Iterable[str],
) -> int:
    """Delete files/shortcuts in a submission folder that are not current revisions."""
    expected = {name for name in expected_names if name}
    if not expected:
        return 0
    deleted = 0
    for child in await drive.list_children(http, token, folder_id):
        if child.get("mimeType") == FOLDER_MIME:
            continue
        if child.get("name") in expected or not child.get("id"):
            continue
        if await drive.delete_file(http, token, child["id"]):
            deleted += 1
    return deleted


async def prune_stale_submission_folders(
    drive: GoogleDriveClient,
    http: httpx.AsyncClient,
    token: str,
    images_folder_id: str,
    expected_submission_ids: Iterable[str],
) -> int:
    """Remove client/internal shortcut folders for submissions no longer exported."""
    expected = {sid for sid in expected_submission_ids if sid}
    if not expected:
        return 0
    deleted = 0
    for child in await drive.list_children(http, token, images_folder_id):
        if child.get("mimeType") != FOLDER_MIME or not child.get("id"):
            continue
        if child.get("name") in expected:
            continue
        deleted += await _delete_tree(drive, http, token, child["id"])
    return deleted


async def clear_submission_folders(
    drive: GoogleDriveClient,
    http: httpx.AsyncClient,
    token: str,
    images_folder_id: str,
) -> int:
    """Remove every submission shortcut folder when a form stops exporting images."""
    deleted = 0
    for child in await drive.list_children(http, token, images_folder_id):
        if child.get("mimeType") != FOLDER_MIME or not child.get("id"):
            continue
        deleted += await _delete_tree(drive, http, token, child["id"])
    return deleted
