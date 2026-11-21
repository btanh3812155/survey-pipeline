"""Time-bound per-form image exceptions for the inter-submission fraud tiers."""
import pytest
from PIL import Image
from psycopg_pool import AsyncConnectionPool

from services.duplicate_detector import check_inter_submission_duplicates, compute_dhash_int
from tests.test_outbox_delivery import database

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _vector(index=0):
    value = [0.0] * 384
    value[index] = 1.0
    return value


def _literal(value):
    return "[" + ",".join(str(item) for item in value) + "]"


async def test_active_form_exception_skips_inter_submission_tiers(database):
    dsn, conn, submission, _event, _read, _users = database
    sid = submission(status="APPROVED")
    image = Image.new("RGB", (32, 32), "white")
    dhash = format(compute_dhash_int(image), "064b")
    embedding = _vector()
    conn.execute(
        "UPDATE survey_submissions SET image_dhash=%s::bit(64), embedding=%s::vector "
        "WHERE submission_id=%s",
        (dhash, _literal(embedding), sid),
    )
    conn.execute(
        "INSERT INTO image_dinov2_exceptions(form_id,image_sha256,embedding,expires_at) "
        "VALUES (%s,%s,%s::vector,NOW()+INTERVAL '1 day')",
        ("TB_TEST", "a" * 64, _literal(embedding)),
    )
    try:
        async with AsyncConnectionPool(dsn, open=False, min_size=1, max_size=2) as pool:
            assert await check_inter_submission_duplicates(
                pool, image, embedding, form_id="TB_TEST",
            ) == (False, None, None)

            conn.execute(
                "UPDATE image_dinov2_exceptions SET expires_at=NOW()-INTERVAL '1 minute' "
                "WHERE form_id='TB_TEST'"
            )
            duplicate, matched, kind = await check_inter_submission_duplicates(
                pool, image, embedding, form_id="TB_TEST",
            )
    finally:
        conn.execute("DELETE FROM image_dinov2_exceptions WHERE form_id='TB_TEST'")

    assert duplicate is True
    assert matched == sid
    assert kind == "IMAGE_DHASH"
