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
from typing import Optional

# Ensure project root is in path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.google_drive_delivery import (
    get_google_oauth_credentials,
    get_google_service_account_info,
    sync_all_approved_to_drive,
    LOCAL_IMAGES_DIR,
    MASTER_CSV_FILE,
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

    submission_dirs = glob.glob(os.path.join(LOCAL_IMAGES_DIR, "*", "submissions", "*"))
    filtered_sub_dirs = []
    image_files = []
    for d in submission_dirs:
        if os.path.isdir(d):
            sub_id = os.path.basename(d)
            if is_test_submission(sub_id):
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

    ok = await sync_all_approved_to_drive()
    if not ok:
        logger.error("Google Drive sync failed (see errors above).")
        return 1
    logger.info(f"✅ Deliverables synced successfully for administrative account: {GOOGLE_ADMIN_EMAIL}")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
