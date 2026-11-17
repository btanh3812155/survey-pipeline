#!/usr/bin/env python3
"""
scripts/share_drive_folder.py

Grants writer access on a Google Drive folder to a user and prints the current
permission list for verification (never prints tokens).

Usage inside the app container:
    python scripts/share_drive_folder.py                        # configured root
    python scripts/share_drive_folder.py --folder-id root       # SA's My Drive root
    python scripts/share_drive_folder.py someone@example.com --folder-id <id>

Defaults to buitheanh1003@gmail.com and GOOGLE_DRIVE_ROOT_FOLDER_ID. Uses the
same credential discovery as the delivery pipeline (OAuth2 refresh token /
service account / gcloud CLI).
"""
import argparse
import asyncio
import json
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import httpx

from services.google_drive_delivery import GoogleDriveClient, has_google_credentials

DEFAULT_SHAREE_EMAIL = "buitheanh1003@gmail.com"
DEFAULT_SERVICE_ACCOUNT_EMAIL = "drive-worker@trust-bridge-514111.iam.gserviceaccount.com"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"

TOKEN_PERMISSION_FIELDS = "id,type,role,emailAddress,displayName"
LIST_PERMISSION_FIELDS = "permissions(id,type,role,emailAddress,displayName)"


async def _resolve_folder_id(http_client, headers, folder_id):  # noqa: ANN001
    resp = await http_client.get(
        f"{DRIVE_FILES_URL}/{folder_id}",
        headers=headers,
        params={"fields": "id,name"},
    )
    if resp.status_code != 200:
        print(f"Cannot access folder '{folder_id}': HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return None, None
    body = resp.json()
    return body.get("id"), body.get("name")


async def share_folder(sharee_emails: list, folder_id: str) -> int:
    if not has_google_credentials():
        print("No Google Drive credentials configured.", file=sys.stderr)
        return 1

    client = GoogleDriveClient()
    folder_id = folder_id or client.root_folder_id
    if not folder_id:
        print("No folder id given and GOOGLE_DRIVE_ROOT_FOLDER_ID is not configured.", file=sys.stderr)
        return 1

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            print("Could not obtain a Google Drive access token.", file=sys.stderr)
            return 1
        headers = {"Authorization": f"Bearer {token}"}

        resolved_id, folder_name = await _resolve_folder_id(http_client, headers, folder_id)
        if not resolved_id:
            return 1
        print(f"Target folder: {folder_name} ({resolved_id})")

        for sharee_email in sharee_emails:
            payload = {
                "type": "user",
                "role": "writer",
                "emailAddress": sharee_email,
                "sendNotificationEmail": True,
            }
            resp = await http_client.post(
                f"{DRIVE_FILES_URL}/{resolved_id}/permissions",
                headers=headers,
                json=payload,
                params={"fields": TOKEN_PERMISSION_FIELDS, "sendNotificationEmail": "true"},
            )
            if resp.status_code not in (200, 201):
                # Service accounts cannot always trigger notification emails; retry silently.
                print(
                    f"Permission create for {sharee_email} failed (HTTP {resp.status_code}); "
                    f"retrying without notification email.",
                    file=sys.stderr,
                )
                payload.pop("sendNotificationEmail", None)
                resp = await http_client.post(
                    f"{DRIVE_FILES_URL}/{resolved_id}/permissions",
                    headers=headers,
                    json=payload,
                    params={"fields": TOKEN_PERMISSION_FIELDS},
                )
            if resp.status_code in (200, 201):
                print(f"Granted writer access on {folder_name} to {sharee_email}")
            elif "already" in resp.text.lower():
                print(f"{sharee_email} already has access to {folder_name}")
            else:
                print(
                    f"Failed to grant access to {sharee_email}: "
                    f"HTTP {resp.status_code} {resp.text[:300]}",
                    file=sys.stderr,
                )
                return 1

        listing = await http_client.get(
            f"{DRIVE_FILES_URL}/{resolved_id}/permissions",
            headers=headers,
            params={"fields": LIST_PERMISSION_FIELDS, "pageSize": 100},
        )
        if listing.status_code != 200:
            print(
                f"Failed to list permissions: HTTP {listing.status_code} {listing.text[:300]}",
                file=sys.stderr,
            )
            return 1
        permissions = listing.json().get("permissions", [])
        print(json.dumps(permissions, ensure_ascii=False, indent=2))
        writers = {
            p.get("emailAddress", "").lower()
            for p in permissions
            if p.get("role") in ("writer", "owner")
        }
        missing = [e for e in sharee_emails if e.lower() not in writers]
        if missing:
            print(f"Verification failed: not writer on {resolved_id}: {', '.join(missing)}", file=sys.stderr)
            return 1
        print(f"Verified writers on {resolved_id}: {', '.join(sharee_emails)}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Share a Google Drive folder for the delivery pipeline")
    parser.add_argument(
        "emails",
        nargs="*",
        help="Google account emails to grant writer access to "
        f"(default: {DEFAULT_SHAREE_EMAIL} and {DEFAULT_SERVICE_ACCOUNT_EMAIL})",
    )
    parser.add_argument(
        "--folder-id",
        default=os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", ""),
        help="Folder id to share (defaults to GOOGLE_DRIVE_ROOT_FOLDER_ID; 'root' = caller's My Drive)",
    )
    args = parser.parse_args()
    emails = args.emails or [
        os.environ.get("GOOGLE_DRIVE_SHARE_EMAIL", DEFAULT_SHAREE_EMAIL),
        DEFAULT_SERVICE_ACCOUNT_EMAIL,
    ]
    return asyncio.run(share_folder(emails, args.folder_id))


if __name__ == "__main__":
    sys.exit(main())
