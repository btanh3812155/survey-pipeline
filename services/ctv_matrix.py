# -*- coding: utf-8 -*-
"""Internal CTV matrix (Google Sheet) generation and Drive sync."""
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

logger = logging.getLogger("ctv_matrix")

FORM_BASE_URL = "https://survey.trustbridgevn.com/forms"
QR_API_URL = "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={data}"
INTERNAL_FOLDER_NAME = "Internal"
CTV_SHEET_NAME = "CTV_Info"
# Workspace Sheets blocks external IMAGE() fetches, so QR PNGs are served by our app
# and the QR column contains a plain (auto-hyperlinked) URL to the hosted PNG.
APP_QR_DIR = os.environ.get("CTV_QR_MEDIA_DIR") or os.path.join(
    os.getcwd(), "data", "media", "forms", "qr"
)
PUBLIC_MEDIA_BASE = os.environ.get(
    "PUBLIC_MEDIA_BASE", "https://survey.trustbridgevn.com/media"
)

CTV_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ctv_directory (
        code TEXT PRIMARY KEY,
        display_name TEXT,
        phone TEXT,
        telegram_user_id BIGINT,
        is_active BOOLEAN DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ctv_form_links (
        ctv_code TEXT,
        form_id TEXT,
        url TEXT,
        qr TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (ctv_code, form_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS form_tags (
        form_id TEXT,
        tag TEXT,
        PRIMARY KEY (form_id, tag)
    );
    """,
]


def _rows_to_dicts(cur, rows: List[Any]) -> List[Dict[str, Any]]:
    columns = [getattr(desc, "name", None) for desc in (getattr(cur, "description", None) or [])]
    records: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            records.append(row)
        elif columns and all(columns):
            records.append(dict(zip(columns, row)))
    return records


def form_link_url(form_id: str, ctv_code: str) -> str:
    return f"{FORM_BASE_URL}/{form_id}?ref={ctv_code}"


def qr_image_url(url: str) -> str:
    return QR_API_URL.format(data=quote(url, safe=""))


def parse_qr_record(value: Any) -> Dict[str, Any]:
    """Normalizes ctv_form_links.qr (JSON, dict or legacy string) into a dict."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError):
        pass
    return {"legacy_url": str(value)}


def qr_record_is_current(record: Dict[str, Any], link_url: str) -> bool:
    return bool(
        record.get("type") in ("app_image", "app_link")
        and record.get("image_url")
        and record.get("target") == link_url
    )


def qr_cell_value(link: Dict[str, Any]) -> str:
    """
    Plain URL text for the app-hosted QR PNG.

    Google Sheets in this Workspace rejects every external IMAGE() with
    "#REF! (Please use a desktop web browser ...)", so a plain URL is used;
    Sheets auto-hyperlinks it and clicking opens the QR image.
    """
    record = parse_qr_record(link.get("qr"))
    image_url = record.get("image_url") or ""
    target = record.get("target") or link.get("url") or ""
    return image_url or target


def qr_file_name(ctv_code: str, form_id: str) -> str:
    safe_code = re.sub(r"[^A-Za-z0-9_.-]+", "_", ctv_code) or "ctv"
    safe_form = re.sub(r"[^A-Za-z0-9_.-]+", "_", form_id) or "form"
    return f"{safe_code}_{safe_form}.png"


async def generate_qr_png(target_url: str, http_client: Any = None) -> Optional[bytes]:
    """Local QR library when available, otherwise fetch a PNG from the QR endpoint."""
    try:
        import qrcode  # type: ignore[import-not-found]

        image = qrcode.make(target_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        pass

    if http_client is None:
        return None
    try:
        resp = await http_client.get(
            qr_image_url(target_url),
            timeout=20.0,
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning(f"QR endpoint returned HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not fetch QR PNG for {target_url}: {e}")
    return None


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ... (works beyond 26 columns)."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


async def ensure_ctv_tables(db_pool: Any) -> None:
    """Defensively creates the CTV tables (idempotent)."""
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            for statement in CTV_DDL:
                await cur.execute(statement)


async def fetch_active_ctvs(db_pool: Any) -> List[Dict[str, Any]]:
    """Active CTVs ordered by code; inactive rows are filtered out even if the query is mocked."""
    if db_pool is None:
        return []
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT code, display_name, phone, is_active FROM ctv_directory "
                "WHERE is_active = TRUE ORDER BY code;"
            )
            rows = _rows_to_dicts(cur, await cur.fetchall())
    return [row for row in rows if row.get("is_active", True)]


async def fetch_forms(db_pool: Any) -> List[str]:
    """
    Forms covered by the matrix: FORM_REGISTRY ids, plus any form referenced by
    form_tags or ctv_form_links. If none can be found, fall back to approved submissions.
    """
    form_ids: List[str] = []
    try:
        from core.state import FORM_REGISTRY

        form_ids.extend(str(form_id) for form_id in FORM_REGISTRY.keys())
    except Exception as e:
        logger.debug(f"FORM_REGISTRY unavailable for matrix forms: {e}")

    if db_pool is not None:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT DISTINCT form_id FROM form_tags "
                        "UNION SELECT DISTINCT form_id FROM ctv_form_links;"
                    )
                    for row in await cur.fetchall():
                        value = row.get("form_id") if isinstance(row, dict) else row[0]
                        if value:
                            form_ids.append(str(value))
        except Exception as e:
            logger.debug(f"Could not read form_tags/ctv_form_links: {e}")

    if not form_ids and db_pool is not None:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT DISTINCT form_id FROM survey_submissions WHERE status = 'APPROVED';"
                    )
                    for row in await cur.fetchall():
                        value = row.get("form_id") if isinstance(row, dict) else row[0]
                        if value:
                            form_ids.append(str(value))
        except Exception as e:
            logger.debug(f"Could not read approved form ids: {e}")

    return sorted({form_id for form_id in form_ids if form_id})


async def fetch_links(db_pool: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if db_pool is None:
        return {}
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT ctv_code, form_id, url, qr FROM ctv_form_links;")
            rows = _rows_to_dicts(cur, await cur.fetchall())
    return {
        (str(row["ctv_code"]), str(row["form_id"])): row
        for row in rows
        if row.get("ctv_code") and row.get("form_id")
    }


async def upsert_links(db_pool: Any, links: List[Tuple[str, str, str, str]]) -> None:
    if not links:
        return
    async with db_pool.connection() as conn:
        async with conn.cursor() as cur:
            for ctv_code, form_id, url, qr in links:
                await cur.execute(
                    """
                    INSERT INTO ctv_form_links (ctv_code, form_id, url, qr, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (ctv_code, form_id) DO UPDATE SET
                        url = EXCLUDED.url,
                        qr = EXCLUDED.qr,
                        updated_at = EXCLUDED.updated_at;
                    """,
                    (ctv_code, form_id, url, qr),
                )


def build_matrix_payload(
    ctvs: List[Dict[str, Any]],
    forms: List[str],
    links: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[List[List[Any]], List[Tuple[int, int]]]:
    """
    Builds sheet values and merged header ranges.

    Layout: [form1 QR | form1 URL | form2 QR | form2 URL | ... | Phone | CTV Code].
    Row 1 is the form header (two columns merged per form), row 2 holds QR/URL labels,
    and every following row is one active CTV (phone immediately left of the code).
    """
    header: List[Any] = []
    subheader: List[Any] = []
    for form_id in forms:
        header.extend([form_id, ""])
        subheader.extend(["QR", "URL"])
    header.extend(["Phone", "CTV Code"])
    subheader.extend(["", ""])

    values: List[List[Any]] = [header, subheader]
    for ctv in ctvs:
        code = str(ctv.get("code") or "")
        phone = "" if ctv.get("phone") is None else str(ctv.get("phone"))
        row: List[Any] = []
        for form_id in forms:
            link = links.get((code, form_id)) or {}
            url = link.get("url") or ""
            row.append(qr_cell_value(link))
            row.append(url)
        row.extend([phone, code])
        values.append(row)

    merge_ranges = [(index * 2, index * 2 + 2) for index in range(len(forms))]
    return values, merge_ranges


def build_merge_requests(sheet_id: int, merge_ranges: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
    return [
        {
            "mergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": start,
                    "endColumnIndex": end,
                },
                "mergeType": "MERGE_ALL",
            }
        }
        for start, end in merge_ranges
    ]


async def sync_internal_ctv_matrix(
    drive_client: Any,
    http_client: Any,
    token: str,
    root_id: Optional[str],
    db_pool: Any,
) -> bool:
    """Creates/updates the Internal/CTV_Info matrix sheet from the CTV tables."""
    if db_pool is None:
        logger.debug("[CTV MATRIX] Skipped: no database pool.")
        return False

    await ensure_ctv_tables(db_pool)
    ctvs = await fetch_active_ctvs(db_pool)
    forms = await fetch_forms(db_pool)
    if not ctvs or not forms:
        logger.info(f"[CTV MATRIX] Skipped: {len(ctvs)} active CTVs, {len(forms)} forms.")
        return True

    internal_folder_id = await drive_client.get_or_create_folder(
        http_client, token, INTERNAL_FOLDER_NAME, root_id
    )
    if not internal_folder_id:
        logger.error("[CTV MATRIX] Could not create/find the Internal folder.")
        return False

    existing_links = await fetch_links(db_pool)
    links: Dict[Tuple[str, str], Dict[str, Any]] = {}
    changed: List[Tuple[str, str, str, str]] = []
    for ctv in ctvs:
        code = str(ctv.get("code") or "")
        if not code:
            continue
        for form_id in forms:
            url = form_link_url(form_id, code)
            previous = existing_links.get((code, form_id)) or {}
            previous_qr = parse_qr_record(previous.get("qr"))
            if (
                not previous.get("url")
                or previous.get("url") != url
                or not qr_record_is_current(previous_qr, url)
            ):
                qr_png = await generate_qr_png(url, http_client)
                filename = qr_file_name(code, form_id)
                image_url = ""
                if qr_png:
                    os.makedirs(APP_QR_DIR, exist_ok=True)
                    with open(os.path.join(APP_QR_DIR, filename), "wb") as qr_file:
                        qr_file.write(qr_png)
                    image_url = f"{PUBLIC_MEDIA_BASE.rstrip('/')}/forms/qr/{filename}"
                if not image_url:
                    # Clicking the plain link still opens a rendered QR image.
                    image_url = qr_image_url(url)
                record = {
                    "type": "app_image" if qr_png else "app_link",
                    "image_url": image_url,
                    "target": url,
                    "source_url": qr_image_url(url),
                }
                changed.append((code, form_id, url, json.dumps(record, ensure_ascii=False)))
            else:
                record = previous_qr
            links[(code, form_id)] = {"url": url, "qr": record}
    await upsert_links(db_pool, changed)

    values, merge_ranges = build_matrix_payload(ctvs, forms, links)

    sheet_id = await drive_client.find_file_or_folder(
        http_client, token, CTV_SHEET_NAME, internal_folder_id
    )
    if not sheet_id:
        sheet_id = await drive_client.create_sheet(
            http_client, token, CTV_SHEET_NAME, internal_folder_id
        )
    if not sheet_id:
        logger.error("[CTV MATRIX] Could not create/find the CTV_Info sheet.")
        return False

    metadata = await drive_client.get_spreadsheet_metadata(http_client, token, sheet_id)
    if not metadata or metadata.get("sheet_id") is None:
        logger.error("[CTV MATRIX] Could not read the CTV_Info sheet metadata.")
        return False
    numeric_sheet_id = metadata["sheet_id"]
    previous_columns = max(int(metadata.get("column_count") or 1), 1)
    previous_rows = max(int(metadata.get("row_count") or 1), 1)

    # Clear and unmerge the previous grid before writing the new (possibly narrower) matrix.
    cleared = await drive_client.clear_sheet_values(
        http_client,
        token,
        sheet_id,
        f"A1:{column_letter(previous_columns - 1)}{previous_rows}",
    )
    if not cleared:
        logger.warning("[CTV MATRIX] Could not clear the previous sheet grid.")
    unmerged = await drive_client.batch_update_spreadsheet(
        http_client,
        token,
        sheet_id,
        [
            {
                "unmergeCells": {
                    "range": {
                        "sheetId": numeric_sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": previous_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": previous_columns,
                    }
                }
            }
        ],
    )
    if not unmerged:
        logger.warning("[CTV MATRIX] Could not unmerge previous header cells.")

    updated = await drive_client.update_sheet_values(
        http_client, token, sheet_id, values, value_input_option="USER_ENTERED"
    )
    if not updated:
        logger.error("[CTV MATRIX] values.update failed for CTV_Info.")
        return False

    merged = await drive_client.batch_update_spreadsheet(
        http_client, token, sheet_id, build_merge_requests(numeric_sheet_id, merge_ranges)
    )
    if not merged:
        logger.warning("[CTV MATRIX] Header merge batchUpdate failed.")

    logger.info(
        f"[CTV MATRIX] Synced {len(ctvs)} active CTVs × {len(forms)} forms "
        f"({len(changed)} links generated/updated) into Internal/{CTV_SHEET_NAME}."
    )
    return True
