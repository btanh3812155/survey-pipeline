"""Intra-submission checks honor the form-scoped image exception."""
import io

from PIL import Image

from services import duplicate_detector as detector


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_intra_exception_skips_visual_and_income_checks(monkeypatch):
    monkeypatch.setattr(detector, "compute_dinov2_embedding", lambda _image: [0.0] * 384)
    raw = _png()
    incomes = {
        "week_1": {"gross_revenue": 100, "net_income": 50, "total_trips": 2},
        "week_2": {"gross_revenue": 100, "net_income": 50, "total_trips": 2},
    }

    duplicate, _ = detector.check_intra_submission_duplicates(
        [raw, raw], weekly_incomes=incomes, income_indices=[0, 1],
    )
    assert duplicate is True

    dhash = detector.compute_dhash_int(Image.open(io.BytesIO(raw)).convert("RGB"))
    duplicate, _ = detector.check_intra_submission_duplicates(
        [raw, raw], weekly_incomes=incomes, income_indices=[0, 1],
        exempt_dhashes=[dhash],
    )
    assert duplicate is False
