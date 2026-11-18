# -*- coding: utf-8 -*-
"""Status-aware Google Drive submission mirror.

Two destinations share one form-agnostic planner:

* Customer side: only statuses in ``customer_statuses`` (APPROVED by default),
  mirrored under ``<root>/<form_id>/submissions/<submission_id>/``.
* Internal side: every status, mirrored under
  ``<root>/Internal/<prefix>/<form_id>/submissions/<STATUS>/<submission_id>/``.

The production sync uses ``internal_root_segments=()`` and
``exclude_test=True`` so real submissions land at
``Internal/<form_id>/submissions/<STATUS>/<id>/`` while TEST_ rows stay in
``Internal/TEST_DATA``. TEST_DATA delivery reuses the same code with
``internal_root_segments=("TEST_DATA",)`` and ``require_test=True``.
"""
import csv
import io
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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

logger = logging.getLogger("drive_submission_mirror")

DEFAULT_INTERNAL_FOLDER = "Internal"
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


def internal_folder_name(env: Optional[Dict[str, str]] = None) -> str:
    return (env or os.environ).get("GOOGLE_DRIVE_INTERNAL_FOLDER") or DEFAULT_INTERNAL_FOLDER


def customer_publish_enabled(env: Optional[Dict[str, str]] = None) -> bool:
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


def plan_submission_mirror(
    records: Iterable[Dict[str, Any]],
    *,
    images_root: Optional[str] = None,
    publish_customer: bool = False,
    customer_statuses: Sequence[str] = ("APPROVED",),
    internal_root_segments: Sequence[str] = (),
    require_test: bool = False,
    exclude_test: bool = False,
) -> List[Dict[str, Any]]:
    """Builds the per-submission delivery plan without touching Google Drive."""
    images_root = images_root or LOCAL_IMAGES_DIR
    allowed_customer = {str(status).upper() for status in customer_statuses}
    plan: List[Dict[str, Any]] = []

    for record in records:
        submission_id = str(record.get("submission_id") or "")
        if not submission_id:
            continue
        is_test = is_test_submission(submission_id)
        if require_test and not is_test:
            continue
        if exclude_test and is_test:
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
                if (publish_customer and status in allowed_customer)
                else None,
                "internal_path": list(internal_root_segments)
                + [form_id, "submissions", status, submission_id],
            }
        )
    return plan


def manifest_rows(plan: List[Dict[str, Any]]) -> List[List[str]]:
    """One manifest row per submission, grouped by form and status order."""
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


def _readme_bytes(plan: List[Dict[str, Any]], publish_customer: bool, prefix: str = "") -> bytes:
    statuses = sorted({item["status"] for item in plan}, key=STATUS_ORDER.index)
    lines = [
        "TEST_DATA - automated test submissions (not real customer data).",
        "",
        f"Submissions: {len(plan)}",
        f"Statuses: {', '.join(statuses) or 'none'}",
        "",
        "Layout:",
        f"  {prefix or ''}<form_id>/submissions/<STATUS>/<submission_id>/<images>",
        f"  {prefix or ''}<form_id>/{MANIFEST_NAME}",
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
    segments: Sequence[str],
) -> Optional[str]:
    current_id = parent_id
    for segment in segments:
        current_id = await client.get_or_create_folder(http_client, token, segment, current_id)
        if not current_id:
            return None
    return current_id


async def mirror_submissions_to_drive(
    records: Iterable[Dict[str, Any]],
    *,
    client: GoogleDriveClient,
    http_client: httpx.AsyncClient,
    token: str,
    root_folder_id: Optional[str] = None,
    images_root: Optional[str] = None,
    publish_customer: bool = False,
    customer_statuses: Sequence[str] = ("APPROVED",),
    internal_root_segments: Sequence[str] = (),
    require_test: bool = False,
    exclude_test: bool = False,
    upload_manifests: bool = False,
    upload_readme: bool = False,
    skip_empty: bool = False,
) -> Dict[str, Any]:
    """Uploads a mirror plan with an already-authenticated client."""
    plan = plan_submission_mirror(
        records,
        images_root=images_root,
        publish_customer=publish_customer,
        customer_statuses=customer_statuses,
        internal_root_segments=internal_root_segments,
        require_test=require_test,
        exclude_test=exclude_test,
    )
    if skip_empty:
        plan = [item for item in plan if item["images"]]
    report: Dict[str, Any] = {
        "publish_customer": publish_customer,
        "root_folder_id": None,
        "internal_root_folder_id": None,
        "submissions": len(plan),
        "forms": {},
        "files": [],
    }
    if not plan:
        logger.info("No submissions to mirror.")
        return report

    root_id = root_folder_id or client.root_folder_id or None
    report["root_folder_id"] = root_id
    internal_id = await client.get_or_create_folder(
        http_client, token, internal_folder_name(), root_id
    )
    if not internal_id:
        raise RuntimeError("Could not create/find the Internal Drive folder.")
    internal_root_id = await _ensure_path(
        client, http_client, token, internal_id, list(internal_root_segments)
    )
    if not internal_root_id:
        raise RuntimeError("Could not create/find the internal mirror root folder.")
    report["internal_root_folder_id"] = internal_root_id
    if upload_readme:
        await client.upload_file(
            http_client, token, README_NAME,
            _readme_bytes(plan, publish_customer, "/".join(internal_root_segments)),
            "text/plain; charset=UTF-8", internal_root_id,
        )

    by_form: Dict[str, List[Dict[str, Any]]] = {}
    for item in plan:
        by_form.setdefault(item["form_id"], []).append(item)

    for form_id in sorted(by_form):
        form_items = by_form[form_id]
        form_report: Dict[str, Any] = {"customer": {}, "internal": {}}
        report["forms"][form_id] = form_report

        internal_form_id = await _ensure_path(
            client, http_client, token, internal_root_id, [form_id]
        )
        if not internal_form_id:
            logger.error("Could not create Internal form folder for %s", form_id)
            continue
        internal_submissions_id = await _ensure_path(
            client, http_client, token, internal_form_id, ["submissions"]
        )
        if not internal_submissions_id:
            logger.error("Could not create Internal submissions folder for %s", form_id)
            continue
        form_report["internal_folder_id"] = internal_form_id

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
                http_client, token, item["status"], internal_submissions_id
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

        if upload_manifests:
            manifest_id = await client.upload_file(
                http_client, token, MANIFEST_NAME, manifest_csv_bytes(form_items),
                "text/csv; charset=UTF-8", internal_form_id,
            )
            form_report["manifest_file_id"] = manifest_id

    report["uploaded_files"] = sum(1 for file in report["files"] if file.get("status") == "uploaded")
    report["present_files"] = sum(1 for file in report["files"] if file.get("status") == "already_present")
    report["failed_files"] = sum(1 for file in report["files"] if file.get("status") == "failed")
    return report


async def sync_submission_mirror(records: Iterable[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
    """Creates its own Drive client/token and mirrors the supplied records."""
    client = GoogleDriveClient()
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        token = await client.get_token(http_client)
        if not token:
            raise RuntimeError("Could not obtain a Google Drive access token for the mirror.")
        return await mirror_submissions_to_drive(
            records, client=client, http_client=http_client, token=token, **kwargs
        )


def folder_url(folder_id: Optional[str]) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""
