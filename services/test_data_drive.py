# -*- coding: utf-8 -*-
"""TEST_DATA Google Drive delivery.

Two destinations, both driven by explicitly test-marked submission ids
(see ``is_test_submission``):

* Customer side: APPROVED test entries only, mirrored under the normal
  ``<root>/<form_id>/submissions/<submission_id>/`` layout. Publishing there is
  opt-in (``GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS``) so a routine production
  sync never pushes test images to the customer.
* Internal side: every test entry, whatever its status, under
  ``<root>/Internal/TEST_DATA/<form_id>/submissions/<STATUS>/<submission_id>/``.
  ``TEST_DATA`` mirrors the customer form/submission layout while keeping
  test data, including REJECTED records, out of real production folders.

The plan builder is pure and form-agnostic: callers pass DB records.
"""
import csv
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from services.google_drive_delivery import (
    IMAGE_FILE_EXTENSIONS,
    LOCAL_IMAGES_DIR,
    GoogleDriveClient,
    _guess_mime,
    _list_remote_files,
    detect_image_mime,
    is_test_submission,
    normalize_image_bytes,
    normalized_image_name,
)

logger = logging.getLogger("test_data_drive")

DEFAULT_INTERNAL_FOLDER = "Internal"
DEFAULT_TEST_DATA_FOLDER = "TEST_DATA"
MANIFEST_NAME = "TEST_manifest.csv"
README_NAME = "README.txt"
STATUS_ORDER = ("APPROVED", "REJECTED", "APPEAL_PENDING", "MANUAL_REVIEW", "UNKNOWN")

MANIFEST_HEADERS = [
    "Trạng Thái",
    "Mã Phiếu",
    "Form ID",
    "CTV",
    "CTV Code",
    "Thời Gian Tạo",
    "Số Ảnh",
    "Tổng Doanh Thu 5 Tuần",
    "Tổng Thực Nhận 5 Tuần",
    "Tổng Số Chuyến 5 Tuần",
    "Lý Do Từ Chối",
]


def internal_folder_name() -> str:
    return os.environ.get("GOOGLE_DRIVE_INTERNAL_FOLDER") or DEFAULT_INTERNAL_FOLDER


def test_data_folder_name() -> str:
    return os.environ.get("GOOGLE_DRIVE_TEST_DATA_FOLDER") or DEFAULT_TEST_DATA_FOLDER


def publish_test_to_customer(env: Optional[Dict[str, str]] = None) -> bool:
    """Customer-side test publishing is an explicit opt-in."""
    raw = (env or os.environ).get("GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _safe_segment(value: Any, fallback: str = "UNKNOWN") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned or fallback


def _status_segment(status: Any) -> str:
    segment = _safe_segment(status, "UNKNOWN").upper()
    return segment if segment in STATUS_ORDER else "UNKNOWN"


def _local_images(images_root: str, form_id: str, submission_id: str) -> List[Dict[str, str]]:
    folder = os.path.join(images_root, form_id, "submissions", submission_id)
    if not os.path.isdir(folder):
        return []
    images = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0 and os.path.splitext(name)[1].lower() in IMAGE_FILE_EXTENSIONS:
            images.append({"path": path, "name": name})
    return images


def plan_test_delivery(
    records: List[Dict[str, Any]],
    images_root: Optional[str] = None,
    publish_customer: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Builds the per-submission delivery plan without touching Google Drive.

    Each item carries its local images plus the destination path segments. The
    customer destination exists only for APPROVED records when publishing is
    enabled; the Internal destination exists for every test status.
    """
    images_root = images_root or LOCAL_IMAGES_DIR
    publish_customer = publish_test_to_customer() if publish_customer is None else bool(publish_customer)
    test_root = test_data_folder_name()
    plan: List[Dict[str, Any]] = []

    for record in records:
        submission_id = str(record.get("submission_id") or "")
        if not submission_id or not is_test_submission(submission_id):
            logger.warning("Skipping non-test submission %r from TEST_DATA delivery", submission_id)
            continue
        form_id = _safe_segment(record.get("form_id") or "unknown_form", "unknown_form")
        status = _status_segment(record.get("status"))
        plan.append(
            {
                "submission_id": submission_id,
                "form_id": form_id,
                "status": status,
                "record": record,
                "images": _local_images(images_root, form_id, submission_id),
                "customer_path": ["form", "submissions", submission_id]
                if (publish_customer and status == "APPROVED")
                else None,
                "internal_path": ["TEST_DATA", form_id, "submissions", status, submission_id],
            }
        )
    return plan


def manifest_rows(plan: List[Dict[str, Any]]) -> List[List[str]]:
    """One manifest row per test submission, grouped by form and status order."""
    ordered = sorted(
        plan,
        key=lambda item: (
            item["form_id"],
            STATUS_ORDER.index(item["status"]),
            str(item["record"].get("created_at") or ""),
        ),
    )
    rows = []
    for item in ordered:
        record = item["record"]
        rows.append(
            [
                item["status"],
                item["submission_id"],
                item["form_id"],
                str(record.get("driver_name") or ""),
                str(record.get("ctv_code") or ""),
                str(record.get("created_at") or ""),
                str(len(item["images"])),
                str(record.get("total_gross_revenue") or 0),
                str(record.get("total_net_income") or 0),
                str(record.get("total_trips") or 0),
                str(record.get("rejection_reason") or ""),
            ]
        )
    return rows


def manifest_csv_bytes(plan: List[Dict[str, Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(MANIFEST_HEADERS)
    writer.writerows(manifest_rows(plan))
    return out.getvalue().encode("utf-8-sig")


def _readme_bytes(plan: List[Dict[str, Any]], publish_customer: bool) -> bytes:
    statuses = sorted({item["status"] for item in plan}, key=STATUS_ORDER.index)
    lines = [
        "TEST_DATA - automated test submissions (not real customer data).",
        "",
        f"Submissions: {len(plan)}",
        f"Statuses: {', '.join(statuses) or 'none'}",
        "",
        "Layout:",
        f"  {test_data_folder_name()}/<form_id>/submissions/<STATUS>/<submission_id>/<images>",
        f"  {test_data_folder_name()}/<form_id>/{MANIFEST_NAME}",
        "",
        "Customer-side publishing: " + ("ENABLED (APPROVED entries only)" if publish_customer else "disabled"),
        "",
        "Customer mirror (when enabled): <root>/<form_id>/submissions/<submission_id>/",
        "Internal mirror keeps every status, including REJECTED and APPEAL_PENDING.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _upload_images(
    client: GoogleDriveClient,
    http_client: httpx.AsyncClient,
    token: str,
    folder_id: str,
    images: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    existing = set((await _list_remote_files(http_client, token, folder_id)).keys())
    uploaded: List[Dict[str, Any]] = []
    for image in images:
        try:
            with open(image["path"], "rb") as image_file:
                raw_bytes = image_file.read()
        except OSError as exc:
            logger.warning("Could not read %s: %s", image["path"], exc)
            continue
        normalized = normalize_image_bytes(raw_bytes)
        payload = normalized if normalized is not None else raw_bytes
        out_name = normalized_image_name(image["name"]) if normalized is not None else os.path.basename(image["name"])
        mime_type = detect_image_mime(payload) or _guess_mime(out_name)
        if out_name in existing:
            uploaded.append({"name": out_name, "folder_id": folder_id, "status": "already_present"})
            continue
        file_id = await client.upload_file(http_client, token, out_name, payload, mime_type, folder_id)
        if file_id:
            existing.add(out_name)
            uploaded.append({"name": out_name, "folder_id": folder_id, "file_id": file_id, "status": "uploaded"})
        else:
            uploaded.append({"name": out_name, "folder_id": folder_id, "status": "failed"})
    return uploaded


async def _ensure_path(
    client: GoogleDriveClient,
    http_client: httpx.AsyncClient,
    token: str,
    parent_id: str,
    segments: List[str],
) -> Optional[Dict[str, str]]:
    current_id = parent_id
    ids = []
    for segment in segments:
        current_id = await client.get_or_create_folder(http_client, token, segment, current_id)
        if not current_id:
            return None
        ids.append(current_id)
    return {"id": current_id, "ids": ids}


async def sync_test_data_to_drive(
    records: List[Dict[str, Any]],
    *,
    images_root: Optional[str] = None,
    publish_customer: Optional[bool] = None,
    root_folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Uploads the TEST_DATA plan and returns a report with folder ids and files."""
    publish_customer = publish_test_to_customer() if publish_customer is None else bool(publish_customer)
    plan = plan_test_delivery(records, images_root=images_root, publish_customer=publish_customer)
    report: Dict[str, Any] = {
        "publish_customer": publish_customer,
        "root_folder_id": None,
        "test_data_folder_id": None,
        "submissions": len(plan),
        "forms": {},
        "files": [],
    }
    if not plan:
        logger.info("No test submissions to deliver.")
        return report

    client = GoogleDriveClient()
    root_id = root_folder_id or client.root_folder_id or None
    report["root_folder_id"] = root_id
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            raise RuntimeError("Could not obtain a Google Drive access token for TEST_DATA delivery.")

        internal_id = await client.get_or_create_folder(
            http_client, token, internal_folder_name(), root_id
        )
        if not internal_id:
            raise RuntimeError("Could not create/find the Internal Drive folder.")
        test_root = await _ensure_path(
            client, http_client, token, internal_id, [test_data_folder_name()]
        )
        if not test_root:
            raise RuntimeError("Could not create/find the TEST_DATA Drive folder.")
        report["test_data_folder_id"] = test_root["id"]
        await client.upload_file(
            http_client, token, README_NAME, _readme_bytes(plan, publish_customer),
            "text/plain; charset=UTF-8", test_root["id"],
        )

        by_form: Dict[str, List[Dict[str, Any]]] = {}
        for item in plan:
            by_form.setdefault(item["form_id"], []).append(item)

        for form_id in sorted(by_form):
            form_items = by_form[form_id]
            form_report: Dict[str, Any] = {"customer": {}, "internal": {}}
            report["forms"][form_id] = form_report

            internal_form = await _ensure_path(
                client, http_client, token, test_root["id"], [form_id]
            )
            if not internal_form:
                logger.error("Could not create Internal form folder for %s", form_id)
                continue
            internal_submissions = await _ensure_path(
                client, http_client, token, internal_form["id"], ["submissions"]
            )
            if not internal_submissions:
                logger.error("Could not create Internal submissions folder for %s", form_id)
                continue
            form_report["internal_folder_id"] = internal_form["id"]

            customer_submissions_id = None
            if publish_customer and any(item["customer_path"] for item in form_items):
                customer_form_id = await client.get_or_create_folder(http_client, token, form_id, root_id)
                if customer_form_id:
                    customer_submissions_id = await client.get_or_create_folder(
                        http_client, token, "submissions", customer_form_id
                    )
                    form_report["customer_folder_id"] = customer_form_id

            for item in form_items:
                status_folder_id = await client.get_or_create_folder(
                    http_client, token, item["status"], internal_submissions["id"]
                )
                internal_folder_id = None
                if status_folder_id:
                    internal_folder_id = await client.get_or_create_folder(
                        http_client, token, item["submission_id"], status_folder_id
                    )
                if internal_folder_id:
                    uploaded = await _upload_images(
                        client, http_client, token, internal_folder_id, item["images"]
                    )
                    form_report["internal"].setdefault(item["status"], {})[item["submission_id"]] = {
                        "folder_id": internal_folder_id,
                        "files": uploaded,
                    }
                    report["files"].extend(
                        dict(file, side="internal", status_group=item["status"], submission_id=item["submission_id"])
                        for file in uploaded
                    )

                if item["customer_path"] and customer_submissions_id:
                    customer_folder_id = await client.get_or_create_folder(
                        http_client, token, item["submission_id"], customer_submissions_id
                    )
                    if customer_folder_id:
                        uploaded = await _upload_images(
                            client, http_client, token, customer_folder_id, item["images"]
                        )
                        form_report["customer"][item["submission_id"]] = {
                            "folder_id": customer_folder_id,
                            "files": uploaded,
                        }
                        report["files"].extend(
                            dict(file, side="customer", status_group=item["status"], submission_id=item["submission_id"])
                            for file in uploaded
                        )

            manifest_id = await client.upload_file(
                http_client, token, MANIFEST_NAME, manifest_csv_bytes(form_items),
                "text/csv; charset=UTF-8", internal_form["id"],
            )
            form_report["manifest_file_id"] = manifest_id

    report["uploaded_files"] = sum(1 for file in report["files"] if file.get("status") == "uploaded")
    report["present_files"] = sum(1 for file in report["files"] if file.get("status") == "already_present")
    report["failed_files"] = sum(1 for file in report["files"] if file.get("status") == "failed")
    return report


def folder_url(folder_id: Optional[str]) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""
