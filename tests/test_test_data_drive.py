# -*- coding: utf-8 -*-
"""TEST_DATA Drive mirror planning and routing."""

import os

from services.test_data_drive import (
    MANIFEST_HEADERS,
    manifest_rows,
    plan_test_delivery,
    publish_test_to_customer,
)


def _record(submission_id, form_id="form_alpha", status="APPROVED", **extra):
    return {
        "submission_id": submission_id,
        "form_id": form_id,
        "status": status,
        "ctv_code": "TEST_CTV_01",
        "driver_name": "TEST_DRIVER",
        "total_gross_revenue": 1000,
        "total_net_income": 800,
        "total_trips": 10,
        **extra,
    }


def _stage_images(root, form_id, submission_id, names):
    folder = root / form_id / "submissions" / submission_id
    folder.mkdir(parents=True)
    for name, payload in names.items():
        (folder / name).write_bytes(payload)


def test_approved_test_entries_reach_customer_only_when_published(tmp_path):
    rec = _record("TEST_APPROVED_1")
    _stage_images(tmp_path, "form_alpha", "TEST_APPROVED_1", {"slot_0.webp": b"x"})

    hidden = plan_test_delivery([rec], images_root=str(tmp_path), publish_customer=False)
    assert hidden[0]["customer_path"] is None
    assert hidden[0]["internal_path"] == [
        "TEST_DATA", "form_alpha", "submissions", "APPROVED", "TEST_APPROVED_1",
    ]

    shown = plan_test_delivery([rec], images_root=str(tmp_path), publish_customer=True)
    assert shown[0]["customer_path"] == ["form", "submissions", "TEST_APPROVED_1"]


def test_non_approved_test_entries_never_reach_customer(tmp_path):
    records = [
        _record("TEST_REJECTED_1", status="REJECTED"),
        _record("TEST_APPEAL_1", status="APPEAL_PENDING"),
        _record("TEST_REVIEW_1", status="MANUAL_REVIEW"),
    ]
    plan = plan_test_delivery(records, images_root=str(tmp_path), publish_customer=True)
    assert all(item["customer_path"] is None for item in plan)
    assert [item["internal_path"][3] for item in plan] == [
        "REJECTED", "APPEAL_PENDING", "MANUAL_REVIEW",
    ]


def test_real_submissions_are_never_in_the_test_plan(tmp_path):
    records = [_record("VN-REAL-1"), _record("TEST_OK_1")]
    plan = plan_test_delivery(records, images_root=str(tmp_path), publish_customer=True)
    assert [item["submission_id"] for item in plan] == ["TEST_OK_1"]


def test_unknown_status_and_form_are_sanitized(tmp_path):
    plan = plan_test_delivery(
        [_record("TEST_WEIRD_1", form_id="../../etc", status="SUSPICIOUS NEEDS REVIEW")],
        images_root=str(tmp_path),
        publish_customer=True,
    )
    assert plan[0]["form_id"] == "etc"
    assert plan[0]["internal_path"][3] == "UNKNOWN"


def test_only_non_empty_image_files_are_planned(tmp_path):
    _stage_images(
        tmp_path, "form_alpha", "TEST_IMAGES_1",
        {"slot_0.webp": b"x", "notes.txt": b"x", "slot_1.webp": b""},
    )
    plan = plan_test_delivery(
        [_record("TEST_IMAGES_1")], images_root=str(tmp_path), publish_customer=True
    )
    assert [image["name"] for image in plan[0]["images"]] == ["slot_0.webp"]


def test_manifest_contains_every_status_without_pii_columns():
    plan = plan_test_delivery(
        [
            _record("TEST_M_1", status="MANUAL_REVIEW"),
            _record("TEST_M_2", status="REJECTED", rejection_reason="TEST_DATA: blur"),
            _record("TEST_M_3", status="APPROVED"),
        ],
        images_root="/nonexistent",
        publish_customer=True,
    )
    rows = manifest_rows(plan)
    assert [row[0] for row in rows] == ["APPROVED", "REJECTED", "MANUAL_REVIEW"]
    assert rows[1][10] == "TEST_DATA: blur"
    assert len(MANIFEST_HEADERS) == len(rows[0])
    assert "Số Điện Thoại" not in MANIFEST_HEADERS


def test_customer_publish_flag_is_opt_in():
    assert publish_test_to_customer({}) is False
    assert publish_test_to_customer({"GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS": "no"}) is False
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert publish_test_to_customer({"GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS": value}) is True
