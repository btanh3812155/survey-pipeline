#!/usr/bin/env python3
"""Backfills the internal Drive mirror for every real submission status.

Creates/updates ``Internal/<form_id>/submissions/<STATUS>/<submission_id>/`` for
APPROVED, REJECTED, APPEAL_PENDING and MANUAL_REVIEW. TEST_ submissions stay in
``Internal/TEST_DATA`` and are excluded here. Customer folders are untouched.

    python scripts/mirror_internal_status.py --dry-run   # counts/size only
    python scripts/mirror_internal_status.py
"""
import argparse
import asyncio
import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mirror_internal_status")

MIRROR_STATUSES = ("APPROVED", "REJECTED", "APPEAL_PENDING", "MANUAL_REVIEW")


def fetch_records(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT submission_id, form_id, status, ctv_code, driver_name, created_at,
                   rejection_reason, total_gross_revenue, total_net_income, total_trips
            FROM survey_submissions
            WHERE status = ANY(%s)
            ORDER BY form_id, status, created_at;
            """,
            (list(MIRROR_STATUSES),),
        )
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def summarize(plan: list) -> dict:
    by_status = {}
    files = 0
    total_bytes = 0
    for item in plan:
        bucket = by_status.setdefault(item["status"], {"submissions": 0, "files": 0, "bytes": 0})
        bucket["submissions"] += 1
        bucket["files"] += len(item["images"])
        size = sum(os.path.getsize(image["path"]) for image in item["images"])
        bucket["bytes"] += size
        files += len(item["images"])
        total_bytes += size
    return {"submissions": len(plan), "files": files, "bytes": total_bytes, "by_status": by_status}


def print_plan(plan: list, summary: dict) -> None:
    print(
        f"internal mirror: {summary['submissions']} submissions, "
        f"{summary['files']} images, {summary['bytes']} bytes"
    )
    for status in sorted(summary["by_status"]):
        bucket = summary["by_status"][status]
        print(
            f"  {status:<15} {bucket['submissions']:>3} submissions "
            f"{bucket['files']:>4} images {bucket['bytes']} bytes"
        )
    for item in plan:
        if not item["images"]:
            continue
        print(f"  {'/'.join(item['internal_path'])}  ({len(item['images'])} files)")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill the internal Google Drive status mirror")
    parser.add_argument("--dry-run", action="store_true", help="Print counts/size without uploading")
    parser.add_argument("--json", action="store_true", help="Print the raw sync report as JSON")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set.")
        return 1

    import psycopg

    from services.drive_submission_mirror import (
        plan_submission_mirror,
        sync_submission_mirror,
    )

    with psycopg.connect(database_url) as conn:
        records = fetch_records(conn)

    plan = plan_submission_mirror(
        records,
        internal_root_segments=(),
        publish_customer=False,
        exclude_test=True,
    )
    summary = summarize(plan)
    print_plan(plan, summary)
    if args.dry_run:
        return 0

    report = await sync_submission_mirror(
        records,
        internal_root_segments=(),
        publish_customer=False,
        exclude_test=True,
        skip_empty=True,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"internal mirror done: uploaded={report.get('uploaded_files', 0)} "
            f"already_present={report.get('present_files', 0)} failed={report.get('failed_files', 0)}"
        )
    return 0 if report.get("failed_files", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
