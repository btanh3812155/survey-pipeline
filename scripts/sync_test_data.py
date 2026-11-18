#!/usr/bin/env python3
"""Seeds clearly marked TEST_DATA submissions and syncs them to Google Drive.

Runs inside the application container (production or local) because it needs the
same DATABASE_URL, Google credentials and bind-mounted data directories.

    python scripts/sync_test_data.py --seed                 # insert + stage images + sync internal
    python scripts/sync_test_data.py --seed --publish-customer
    python scripts/sync_test_data.py --seed --seed-only

Every row uses a ``TEST_`` submission id and ``is_test_mode: true`` in form_data,
so real submissions are never touched. Re-running is idempotent (deterministic ids,
upserts, image folders rewritten from samples).
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.google_drive_delivery import (  # noqa: E402
    LOCAL_IMAGES_DIR,
    SUBMISSION_PHOTOS_DIR,
    is_test_submission,
    normalize_image_bytes,
    normalized_image_name,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_test_data")

TEST_SAMPLES_DIR = os.path.join(PROJECT_ROOT, "data", "test_samples")
SAMPLE_POOL = [
    "official_passing_survey_receipt.jpg",
    "slot0_profile.png",
    "slot2_week1.png",
    "slot3_week2.png",
    "slot4_week3.png",
    "slot_1_profile.jpg",
    "slot_2_vehicle.jpg",
    "gsm_sample_bike.jpg",
    "gsm_sample_car_crop.jpg",
    "sample_income_report.jpg",
]

FORM_GSM = "TB_GSM_2026_Nov_Test"
FORM_INCOME_TEST = "income_survey_test"

TEST_SUBMISSIONS = [
    {
        "submission_id": "TEST_TB_001_APPROVED",
        "form_id": FORM_GSM,
        "status": "APPROVED",
        "ctv_code": "TEST_CTV_HANOI_01",
        "driver_name": "TEST_DRIVER_ALPHA",
        "vehicle_type": "CAR",
        "car_model": "VF 8",
        "gsm_duration": "12 tháng",
        "gsm_contract_type": "Toàn thời gian",
        "daily_trips": 26,
        "monthly_income_range": "25-30 triệu",
        "gsm_incentive_policy": "Thưởng 500k khi đạt 600 chuyến/tháng",
        "membership_type": "Bạch Kim",
        "weeks": [(4_200_000, 3_400_000, 152), (4_050_000, 3_250_000, 148), (4_400_000, 3_550_000, 160), (4_150_000, 3_300_000, 151), (4_300_000, 3_450_000, 156)],
        "images": 3,
        "created_days_ago": 1,
        "notes": "TEST_DATA approved, full 5 weeks, 3 images",
    },
    {
        "submission_id": "TEST_TB_002_APPROVED",
        "form_id": FORM_GSM,
        "status": "APPROVED",
        "ctv_code": "TEST_CTV_HCM_02",
        "driver_name": "TEST_DRIVER_BRAVO",
        "vehicle_type": "BIKE",
        "car_model": "VinFast Evo200",
        "gsm_duration": "6 tháng",
        "gsm_contract_type": "Bán thời gian",
        "daily_trips": 18,
        "monthly_income_range": "15-20 triệu",
        "gsm_incentive_policy": "Không có",
        "membership_type": "Tiêu chuẩn",
        "weeks": [(2_100_000, 1_750_000, 96), (2_250_000, 1_880_000, 102), (2_050_000, 1_700_000, 94), (2_180_000, 1_820_000, 99)],
        "images": 1,
        "created_days_ago": 2,
        "notes": "TEST_DATA approved, 4 weeks only, single image",
    },
    {
        "submission_id": "TEST_TB_003_REJECTED",
        "form_id": FORM_GSM,
        "status": "REJECTED",
        "ctv_code": "TEST_CTV_HCM_02",
        "driver_name": "TEST_DRIVER_CHARLIE",
        "vehicle_type": "CAR",
        "car_model": "VF 5",
        "gsm_duration": "12 tháng",
        "gsm_contract_type": "Toàn thời gian",
        "daily_trips": 22,
        "monthly_income_range": "20-25 triệu",
        "gsm_incentive_policy": "Thưởng theo tuần",
        "membership_type": "Vàng",
        "weeks": [(3_100_000, 2_500_000, 121), (3_050_000, 2_450_000, 118), (3_300_000, 2_650_000, 130), (3_150_000, 2_520_000, 124), (3_200_000, 2_560_000, 126)],
        "images": 2,
        "created_days_ago": 3,
        "rejection_reason": "TEST_DATA: ảnh thu nhập bị mờ, không đọc được số tuần 3",
        "notes": "TEST_DATA rejected, 2 images",
    },
    {
        "submission_id": "TEST_TB_004_APPEAL",
        "form_id": FORM_GSM,
        "status": "APPEAL_PENDING",
        "ctv_code": "TEST_CTV_DANANG_03",
        "driver_name": "TEST_DRIVER_DELTA",
        "vehicle_type": "CAR",
        "car_model": "VF 6",
        "gsm_duration": "9 tháng",
        "gsm_contract_type": "Toàn thời gian",
        "daily_trips": 30,
        "monthly_income_range": "30-35 triệu",
        "gsm_incentive_policy": "Thưởng 1 triệu khi đạt 700 chuyến",
        "membership_type": "Kim Cương",
        "weeks": [(4_800_000, 3_900_000, 176), (4_900_000, 3_980_000, 181), (5_100_000, 4_150_000, 188), (4_950_000, 4_020_000, 183), (5_000_000, 4_060_000, 186)],
        "images": 2,
        "created_days_ago": 4,
        "notes": "TEST_DATA appeal pending after manual review",
    },
    {
        "submission_id": "TEST_TB_005_REVIEW",
        "form_id": FORM_GSM,
        "status": "MANUAL_REVIEW",
        "ctv_code": "TEST_CTV_HANOI_01",
        "driver_name": "TEST_DRIVER_ECHO",
        "vehicle_type": "BIKE",
        "car_model": "Dat Bike Weaver",
        "gsm_duration": "3 tháng",
        "gsm_contract_type": "Bán thời gian",
        "daily_trips": 14,
        "monthly_income_range": "10-15 triệu",
        "gsm_incentive_policy": "Không có",
        "membership_type": "Tiêu chuẩn",
        "weeks": [(1_500_000, 1_220_000, 71), (1_620_000, 1_310_000, 76), (1_480_000, 1_200_000, 69)],
        "images": 1,
        "created_days_ago": 5,
        "notes": "TEST_DATA routed to manual review (suspicious/needs review)",
    },
    {
        "submission_id": "TEST_IS_001_APPROVED",
        "form_id": FORM_INCOME_TEST,
        "status": "APPROVED",
        "ctv_code": "TEST_CTV_HCM_02",
        "driver_name": "TEST_DRIVER_FOXTROT",
        "vehicle_type": "CAR",
        "car_model": "VF 9",
        "gsm_duration": "12 tháng",
        "gsm_contract_type": "Toàn thời gian",
        "daily_trips": 24,
        "monthly_income_range": "25-30 triệu",
        "gsm_incentive_policy": "Thưởng hiệu suất quý",
        "membership_type": "Bạch Kim",
        "weeks": [(3_800_000, 3_050_000, 138), (3_900_000, 3_120_000, 141), (3_750_000, 3_000_000, 135), (3_850_000, 3_080_000, 139), (3_950_000, 3_160_000, 143)],
        "images": 2,
        "created_days_ago": 2,
        "notes": "TEST_DATA second form approved",
    },
    {
        "submission_id": "TEST_IS_002_REJECTED",
        "form_id": FORM_INCOME_TEST,
        "status": "REJECTED",
        "ctv_code": "TEST_CTV_DANANG_03",
        "driver_name": "TEST_DRIVER_GOLF",
        "vehicle_type": "BIKE",
        "car_model": "VinFast Feliz",
        "gsm_duration": "6 tháng",
        "gsm_contract_type": "Bán thời gian",
        "daily_trips": 16,
        "monthly_income_range": "10-15 triệu",
        "gsm_incentive_policy": "Không có",
        "membership_type": "Tiêu chuẩn",
        "weeks": [(1_800_000, 1_460_000, 84), (1_750_000, 1_420_000, 82)],
        "images": 1,
        "created_days_ago": 3,
        "rejection_reason": "TEST_DATA: ảnh chụp màn hình thay vì ảnh gốc",
        "notes": "TEST_DATA second form rejected",
    },
    {
        "submission_id": "TEST_IS_003_APPEAL",
        "form_id": FORM_INCOME_TEST,
        "status": "APPEAL_PENDING",
        "ctv_code": "TEST_CTV_HANOI_01",
        "driver_name": "TEST_DRIVER_HOTEL",
        "vehicle_type": "CAR",
        "car_model": "VF 7",
        "gsm_duration": "9 tháng",
        "gsm_contract_type": "Toàn thời gian",
        "daily_trips": 27,
        "monthly_income_range": "30-35 triệu",
        "gsm_incentive_policy": "Thưởng 700k khi đạt 650 chuyến",
        "membership_type": "Vàng",
        "weeks": [(4_400_000, 3_560_000, 158), (4_350_000, 3_520_000, 156), (4_500_000, 3_640_000, 161), (4_420_000, 3_580_000, 159), (4_480_000, 3_620_000, 160)],
        "images": 1,
        "created_days_ago": 4,
        "notes": "TEST_DATA second form appeal pending",
    },
    {
        "submission_id": "TEST_IS_004_REVIEW",
        "form_id": FORM_INCOME_TEST,
        "status": "MANUAL_REVIEW",
        "ctv_code": "TEST_CTV_HANOI_01",
        "driver_name": "TEST_DRIVER_INDIA",
        "vehicle_type": "CAR",
        "car_model": "VF 8",
        "gsm_duration": "12 tháng",
        "gsm_contract_type": "Toàn thời gian",
        "daily_trips": 20,
        "monthly_income_range": "20-25 triệu",
        "gsm_incentive_policy": "Không có",
        "membership_type": "Tiêu chuẩn",
        "weeks": [(3_000_000, 2_430_000, 110), (2_950_000, 2_390_000, 108)],
        "images": 2,
        "created_days_ago": 5,
        "notes": "TEST_DATA second form manual review",
    },
]

INSERT_COLUMNS = [
    "submission_id", "form_id", "ctv_code", "driver_phone", "driver_name",
    "receipt_serial_no", "store_tax_id", "gps_latitude", "gps_longitude",
    "status", "rejection_reason", "moderation_notes", "moderated_at",
    "form_data", "raw_extracted_facts", "vehicle_type", "car_model",
    "gsm_duration", "gsm_contract_type", "daily_trips", "monthly_income_range",
    "gsm_incentive_policy", "membership_type",
    "income_week_1_gross", "income_week_1_net", "income_week_1_trips",
    "income_week_2_gross", "income_week_2_net", "income_week_2_trips",
    "income_week_3_gross", "income_week_3_net", "income_week_3_trips",
    "income_week_4_gross", "income_week_4_net", "income_week_4_trips",
    "income_week_5_gross", "income_week_5_net", "income_week_5_trips",
    "total_gross_revenue", "total_net_income", "total_trips",
    "created_at", "action_status",
]


def _record_row(spec: dict, index: int) -> dict:
    weeks = list(spec.get("weeks") or [])
    weeks += [(0, 0, 0)] * (5 - len(weeks))
    created_at = datetime.now(timezone.utc) - timedelta(days=spec.get("created_days_ago", 1))
    row = {
        "submission_id": spec["submission_id"],
        "form_id": spec["form_id"],
        "ctv_code": spec["ctv_code"],
        "driver_phone": f"TEST-{index:08d}",
        "driver_name": spec["driver_name"],
        "receipt_serial_no": f"TEST-SN-{index:03d}",
        "store_tax_id": f"TEST-TAX-{index:03d}",
        "gps_latitude": 21.0278 + index * 0.001,
        "gps_longitude": 105.8342 + index * 0.001,
        "status": spec["status"],
        "rejection_reason": spec.get("rejection_reason"),
        "moderation_notes": spec.get("notes"),
        "moderated_at": created_at if spec["status"] != "MANUAL_REVIEW" else None,
        "form_data": json.dumps(
            {
                "is_test_mode": True,
                "test_marker": "TEST_DATA",
                "vehicle_type": spec["vehicle_type"],
                "car_model": spec["car_model"],
                "gsm_duration": spec["gsm_duration"],
                "gsm_contract_type": spec["gsm_contract_type"],
                "daily_trips": spec["daily_trips"],
                "monthly_income_range": spec["monthly_income_range"],
                "gsm_incentive_policy": spec["gsm_incentive_policy"],
                "membership_type": spec["membership_type"],
                "notes": spec.get("notes"),
            },
            ensure_ascii=False,
        ),
        "raw_extracted_facts": json.dumps(
            {
                "test_marker": "TEST_DATA",
                "is_test_mode": True,
                "weekly_incomes": {
                    f"week_{i + 1}": {"gross_revenue": g, "net_income": n, "total_trips": t}
                    for i, (g, n, t) in enumerate(weeks)
                },
            },
            ensure_ascii=False,
        ),
        "vehicle_type": spec["vehicle_type"],
        "car_model": spec["car_model"],
        "gsm_duration": spec["gsm_duration"],
        "gsm_contract_type": spec["gsm_contract_type"],
        "daily_trips": spec["daily_trips"],
        "monthly_income_range": spec["monthly_income_range"],
        "gsm_incentive_policy": spec["gsm_incentive_policy"],
        "membership_type": spec["membership_type"],
        "total_gross_revenue": sum(w[0] for w in weeks),
        "total_net_income": sum(w[1] for w in weeks),
        "total_trips": sum(w[2] for w in weeks),
        "created_at": created_at,
        "action_status": json.dumps({"test_data": True, "status": spec["status"]}),
    }
    for i, (gross, net, trips) in enumerate(weeks):
        row[f"income_week_{i + 1}_gross"] = gross
        row[f"income_week_{i + 1}_net"] = net
        row[f"income_week_{i + 1}_trips"] = trips
    return row


def stage_images(spec: dict, index: int) -> list:
    """Copies sample images into the deliverables mirror and raw photo store."""
    form_id = spec["form_id"]
    submission_id = spec["submission_id"]
    count = int(spec.get("images") or 1)
    targets = [
        os.path.join(LOCAL_IMAGES_DIR, form_id, "submissions", submission_id),
        os.path.join(SUBMISSION_PHOTOS_DIR, submission_id),
    ]
    for target in targets:
        os.makedirs(target, exist_ok=True)
        for stale in os.listdir(target):
            if stale.startswith("slot_"):
                try:
                    os.remove(os.path.join(target, stale))
                except OSError:
                    pass
    written = []
    for i in range(count):
        sample_name = SAMPLE_POOL[(index + i) % len(SAMPLE_POOL)]
        sample_path = os.path.join(TEST_SAMPLES_DIR, sample_name)
        if not os.path.isfile(sample_path):
            logger.warning("Sample image missing: %s", sample_path)
            continue
        with open(sample_path, "rb") as sample_file:
            raw_bytes = sample_file.read()
        normalized = normalize_image_bytes(raw_bytes)
        payload = normalized if normalized is not None else raw_bytes
        out_name = normalized_image_name(f"slot_{i}{os.path.splitext(sample_name)[1]}") if normalized is not None else f"slot_{i}{os.path.splitext(sample_name)[1]}"
        for target in targets:
            with open(os.path.join(target, out_name), "wb") as out_file:
                out_file.write(payload)
        written.append(os.path.join(targets[0], out_name))
    return written


def seed_submissions(conn) -> list:
    upsert_sql = (
        f"INSERT INTO survey_submissions ({', '.join(INSERT_COLUMNS)}) "
        f"VALUES ({', '.join(['%s'] * len(INSERT_COLUMNS))}) "
        f"ON CONFLICT (submission_id) DO UPDATE SET "
        + ", ".join(f"{col} = EXCLUDED.{col}" for col in INSERT_COLUMNS if col != "submission_id")
        + ";"
    )
    rows = []
    with conn.cursor() as cur:
        for index, spec in enumerate(TEST_SUBMISSIONS, start=1):
            row = _record_row(spec, index)
            values = [row[col] for col in INSERT_COLUMNS]
            cur.execute(upsert_sql, values)
            images = stage_images(spec, index)
            rows.append(
                {
                    "submission_id": spec["submission_id"],
                    "form_id": spec["form_id"],
                    "status": spec["status"],
                    "ctv_code": spec["ctv_code"],
                    "images": len(images),
                }
            )
    conn.commit()
    return rows


def fetch_test_records(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT submission_id, form_id, status, ctv_code, driver_name, created_at,
                   rejection_reason, total_gross_revenue, total_net_income, total_trips
            FROM survey_submissions
            WHERE upper(submission_id) LIKE '%TEST%'
               OR upper(submission_id) LIKE '%DEV%'
               OR upper(submission_id) LIKE '%MOCK%'
               OR upper(submission_id) LIKE '%SAMPLE%'
               OR upper(submission_id) LIKE '%DEMO%'
            ORDER BY form_id, created_at;
            """
        )
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall() if is_test_submission(row[0])]


def print_report(report: dict) -> None:
    from services.drive_submission_mirror import folder_url

    print(f"publish_customer: {report['publish_customer']}")
    print(f"TEST_DATA root: {folder_url(report.get('internal_root_folder_id'))}")
    for form_id, form_report in report.get("forms", {}).items():
        print(f"  form {form_id}")
        if form_report.get("customer_folder_id"):
            print(f"    customer submissions: {folder_url(form_report['customer_folder_id'])}")
        for status, submissions in sorted((form_report.get("internal") or {}).items()):
            for submission_id, folder in sorted(submissions.items()):
                print(f"    internal {status:<15} {submission_id}: {folder_url(folder['folder_id'])}")
        for submission_id, folder in sorted((form_report.get("customer") or {}).items()):
            print(f"    customer APPROVED {submission_id}: {folder_url(folder['folder_id'])}")
    print(
        f"uploaded={report.get('uploaded_files', 0)} "
        f"already_present={report.get('present_files', 0)} "
        f"failed={report.get('failed_files', 0)}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed and sync TEST_DATA submissions to Google Drive")
    parser.add_argument("--seed", action="store_true", help="Insert/update TEST_ rows and stage sample images")
    parser.add_argument("--seed-only", action="store_true", help="Seed and exit without syncing")
    parser.add_argument("--publish-customer", action="store_true", help="Also upload APPROVED test entries to the customer Drive")
    parser.add_argument("--json", action="store_true", help="Print the raw sync report as JSON")
    args = parser.parse_args()

    if args.publish_customer:
        os.environ["GOOGLE_DRIVE_PUBLISH_TEST_SUBMISSIONS"] = "1"

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set.")
        return 1

    import psycopg

    with psycopg.connect(database_url) as conn:
        if args.seed or args.seed_only:
            seeded = seed_submissions(conn)
            logger.info("Seeded/updated %d TEST_DATA submissions", len(seeded))
            for row in seeded:
                print(f"seeded {row['submission_id']} {row['form_id']} {row['status']} ctv={row['ctv_code']} images={row['images']}")
            if args.seed_only:
                return 0
        records = fetch_test_records(conn)

    from services.drive_submission_mirror import (
        customer_publish_enabled,
        sync_submission_mirror,
    )

    test_data_folder = os.environ.get("GOOGLE_DRIVE_TEST_DATA_FOLDER") or "TEST_DATA"
    report = await sync_submission_mirror(
        records,
        internal_root_segments=(test_data_folder,),
        publish_customer=customer_publish_enabled(),
        require_test=True,
        upload_manifests=True,
        upload_readme=True,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    return 0 if report.get("failed_files", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
