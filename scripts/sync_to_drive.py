#!/usr/bin/env python3
"""
scripts/sync_to_drive.py
Syncs local structured deliverables (Master CSV spreadsheet + image subfolders) to Google Drive.
Supports OAuth 2.0 user credentials, Service Account JSON, and gcloud CLI fallback.

IMPORTANT:
Google Drive sync is an autonomous SERVER-SIDE process running inside the production container
`survey-pipeline-app-1` on the production droplet (146.190.201.32).
Do NOT run production data sync from your local workstation.

To trigger a manual sync on the server, execute remotely:
    ssh root@146.190.201.32 "docker exec -it survey-pipeline-app-1 python scripts/sync_to_drive.py"

Server-side Usage (inside container):
    python scripts/sync_to_drive.py [--dry-run]
"""
import os
import sys
import glob
import logging
import argparse
from typing import Optional, Dict, Any
from datetime import datetime, timezone

# Ensure project root is in path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.storage_r2 import r2_client
from services.google_drive_delivery import (
    GoogleDriveClient,
    get_google_oauth_credentials,
    get_google_service_account_info,
    has_google_credentials,
    LOCAL_DELIVERABLES_DIR,
    LOCAL_IMAGES_DIR,
    MASTER_CSV_FILE,
    CSV_HEADERS,
    extract_survey_row_data,
    GOOGLE_ADMIN_EMAIL,
    is_test_submission,
)
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_to_drive")


def check_credentials() -> bool:
    """Checks for OAuth2 credentials or Service Account and logs status."""
    oauth = get_google_oauth_credentials()
    if oauth:
        acc = oauth.get("account", GOOGLE_ADMIN_EMAIL)
        has_rf = bool(oauth.get("refresh_token"))
        logger.info(f"✅ Google Drive OAuth2 credentials found (account: {acc}, refresh_token: {'yes' if has_rf else 'no'})")
        return True

    sa_info = get_google_service_account_info()
    if sa_info:
        logger.info(f"✅ Google service account found: {sa_info.get('client_email', 'unknown')}")
        return True

    logger.warning("⚠️ No Google Drive OAuth2 or Service Account credentials found.")
    logger.warning("To authorize admin@trustbridgevn.com, run:")
    logger.warning("    python3 scripts/setup_drive_oauth.py")
    return False


def get_approved_submission_ids() -> set:
    """
    Returns set of submission IDs that are APPROVED from DB and local dev storage.
    """
    approved_ids = set()
    # 1. Local dev storage
    dev_storage_file = os.path.join(PROJECT_ROOT, "data", "dev_test_submissions.json")
    if os.path.exists(dev_storage_file):
        try:
            with open(dev_storage_file, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if r.get("status") == "APPROVED" and r.get("submission_id"):
                    approved_ids.add(r.get("submission_id"))
        except Exception as e:
            logger.debug(f"Could not read dev storage for approved IDs: {e}")

    # 2. Database if available
    try:
        import psycopg
        db_url = getattr(settings, "DATABASE_URL", None) or os.environ.get("DATABASE_URL")
        if db_url:
            with psycopg.connect(db_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT submission_id FROM survey_submissions WHERE status = 'APPROVED';")
                    for row in cur.fetchall():
                        if row[0]:
                            approved_ids.add(row[0])
    except Exception as e:
        logger.debug(f"Could not query DB for approved IDs: {e}")

    return approved_ids


def summarize_deliverables(approved_ids: Optional[set] = None):
    csv_rows = 0
    if os.path.exists(MASTER_CSV_FILE):
        with open(MASTER_CSV_FILE, "r", encoding="utf-8-sig", errors="ignore") as f:
            csv_rows = sum(1 for line in f if line.strip()) - 1

    submission_dirs = glob.glob(os.path.join(LOCAL_IMAGES_DIR, "*"))
    filtered_sub_dirs = []
    image_files = []
    for d in submission_dirs:
        if os.path.isdir(d):
            sub_id = os.path.basename(d)
            if sub_id.startswith(("DEV-", "TEST-", "MOCK-")):
                continue
            files = [f for f in glob.glob(os.path.join(d, "*.*")) if os.path.isfile(f) and os.path.getsize(f) > 0]
            if not files:
                continue
            if approved_ids is not None and len(approved_ids) > 0 and sub_id not in approved_ids:
                continue
            filtered_sub_dirs.append(d)
            image_files.extend(files)
    logger.info("📦 Local Deliverables Mirror Summary:")
    logger.info(f"   - Master CSV: {MASTER_CSV_FILE} ({max(csv_rows, 0)} records)")
    logger.info(f"   - Approved Image Subfolders: {len(filtered_sub_dirs)} submissions")
    logger.info(f"   - Total Approved Image Files: {len(image_files)} files")
    return csv_rows, filtered_sub_dirs, image_files

async def main():
    parser = argparse.ArgumentParser(description="Sync deliverables to Google Drive")
    parser.add_argument("--dry-run", action="store_true", help="Simulate upload without pushing files")
    args = parser.parse_args()

    logger.info("Starting Google Drive Deliverables Push / Sync Check...")
    has_creds = check_credentials()
    approved_ids = get_approved_submission_ids()
    csv_rows, sub_dirs, image_files = summarize_deliverables(approved_ids=approved_ids)

    if args.dry_run:
        logger.info("🔎 [DRY RUN] Would sync the following to Google Drive:")
        logger.info(f"   - Master CSV: {MASTER_CSV_FILE}")
        logger.info(f"   - {len(sub_dirs)} submission folders ({len(image_files)} total images)")
        return 0

    if not has_creds:
        logger.info("Local deliverables are completely mirrored and ready for Google Drive.")
        logger.info("To push deliverables to Google Drive, authorize by running:")
        logger.info("    python3 scripts/setup_drive_oauth.py")
        return 0

    import httpx
    client = GoogleDriveClient()
    async with httpx.AsyncClient(timeout=45.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            logger.error("Failed to obtain Google Drive access token.")
            logger.info("Please run: python3 scripts/setup_drive_oauth.py")
            return 1

        logger.info("Successfully obtained Google Drive OAuth2 token. Starting sync...")
        root_id = client.root_folder_id or None
        # Clean up test folders, duplicate CSVs, and summary files from Google Drive
        try:
            await client.cleanup_drive_clutter(http_client, token)
        except Exception as e:
            logger.warning(f"Failed to cleanup clutter from Google Drive: {e}")

        # 1. Upload Master Sheet
        if os.path.exists(MASTER_CSV_FILE):
            with open(MASTER_CSV_FILE, "rb") as f:
                content = f.read()
            file_id = await client.upload_file(
                http_client, token, "Master_Survey_Deliverables.csv", content, "text/csv; charset=UTF-8", root_id
            )
            logger.info(f"📊 Uploaded Master CSV to Drive. File ID: {file_id}")
            # Archive last sheet to R2
            try:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                archive_name = f"Master_Survey_Deliverables_{timestamp}.csv"
                await r2_client.archive_sheet(archive_name, content)
                logger.info(f"📦 [R2 SHEET ARCHIVE] Archived last sheet to R2 as archives/sheets/{archive_name}")
            except Exception as e:
                logger.error(f"Failed to archive master sheet to R2: {e}")

        # 2. Upload image subfolders with directory diffing
        if sub_dirs:
            # Create or resolve top-level "Images" folder on Drive
            images_parent_id = await client.get_or_create_folder(http_client, token, "Images", root_id)

            # Query all existing remote folders in Drive Images/ in a single fast call
            remote_subfolders = {}
            q_sub = f"'{images_parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            url_files = "https://www.googleapis.com/drive/v3/files"
            headers = {"Authorization": f"Bearer {token}"}
            params = {"q": q_sub, "fields": "nextPageToken, files(id, name)", "pageSize": 1000}
            page_tok = None
            while True:
                if page_tok:
                    params["pageToken"] = page_tok
                resp_sub = await http_client.get(url_files, params=params, headers=headers)
                if resp_sub.status_code != 200:
                    break
                d_sub = resp_sub.json()
                for f in d_sub.get("files", []):
                    remote_subfolders[f["name"]] = f["id"]
                page_tok = d_sub.get("nextPageToken")
                if not page_tok:
                    break

            uploaded_count = 0
            skipped_count = 0
            for s_dir in sub_dirs:
                if not os.path.isdir(s_dir):
                    continue
                sub_id = os.path.basename(s_dir)
                if is_test_submission(sub_id):
                    continue

                # Diff check: If folder already exists on Google Drive, skip it!
                if sub_id in remote_subfolders:
                    skipped_count += 1
                    continue

                files = [f for f in glob.glob(os.path.join(s_dir, "*.*")) if os.path.isfile(f) and os.path.getsize(f) > 0]
                if not files:
                    continue
                sub_folder_id = await client.create_folder(http_client, token, sub_id, images_parent_id)

                for fpath in files:
                    fname = os.path.basename(fpath)
                    with open(fpath, "rb") as img_f:
                        img_bytes = img_f.read()
                    mtype = "image/webp" if fname.endswith(".webp") else "application/octet-stream"
                    await client.upload_file(http_client, token, fname, img_bytes, mtype, sub_folder_id)
                    uploaded_count += 1
            logger.info(f"🖼️ Diffed image dirs: {skipped_count} folders up-to-date (skipped); synced {uploaded_count} new files across new subfolders.")
        logger.info(f"✅ Deliverables synced successfully for administrative account: {GOOGLE_ADMIN_EMAIL}")

    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
