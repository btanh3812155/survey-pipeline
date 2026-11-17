"""Telegram dossier must recover income from raw_extracted_facts for legacy rows.

Older submissions have zeroed income_week_* / total_* DB columns; the real
values only exist in raw_extracted_facts.weekly_incomes and its totals.
"""
import json
import pytest

from services.admin_moderator import query_submission_dossier


RAW_FACTS = {
    "total_gross_revenue": 22621000,
    "total_net_income": 5355529,
    "total_trips_count": 253,
    "weekly_incomes": {
        "week_1": {"period": "03/10 - 09/10/2026", "gross_revenue": 5184500, "net_income": 1243078, "total_trips": 61},
        "week_2": {"period": "10/10 - 16/10/2026", "gross_revenue": 4659500, "net_income": 1097313, "total_trips": 48},
        "week_3": {"period": "24/10 - 25/10/2026", "gross_revenue": 1602000, "net_income": 393928, "total_trips": 22},
        "week_4": {"period": "26/10 - 02/11/2026", "gross_revenue": 5160500, "net_income": 1210380, "total_trips": 62},
        "week_5": {"period": "03/11 - 09/11/2026", "gross_revenue": 6014500, "net_income": 1410830, "total_trips": 60},
    },
}


def _row(raw_facts, total_gross=0, total_net=0, total_trips=0, weekly_gross=(0, 0, 0, 0, 0)):
    """31-column survey_submissions row matching query_submission_dossier's SELECT."""
    row = [None] * 31
    row[0] = "VN-LEGACY"
    row[5] = "APPROVED"
    row[7] = raw_facts
    row[14] = None
    row[17] = total_gross
    row[18] = total_net
    row[19] = total_trips
    row[20] = "CAR"
    row[21] = "VF5"
    row[22] = {}
    for i, gross in enumerate(weekly_gross):
        row[23 + i] = gross
    return tuple(row)


class _Cursor:
    def __init__(self, row):
        self.row = row

    async def execute(self, *args, **kwargs):
        return None

    async def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        cursor = _Cursor(self.row)

        class _Ctx:
            async def __aenter__(self):
                return cursor

            async def __aexit__(self, *args):
                return False

        return _Ctx()


class _Pool:
    def __init__(self, row):
        self.row = row

    def connection(self):
        conn = _Conn(self.row)

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_db_zero_columns_are_recovered_from_raw_facts():
    dossier = await query_submission_dossier("VN-LEGACY", db_pool=_Pool(_row(RAW_FACTS)))

    assert dossier["gross_income"] == 22621000
    assert dossier["net_income"] == 5355529
    assert dossier["trips_count"] == 253
    for week, expected in RAW_FACTS["weekly_incomes"].items():
        got = dossier["weekly_incomes"][week]
        assert got["gross_revenue"] == expected["gross_revenue"]
        assert got["net_income"] == expected["net_income"]
        assert got["total_trips"] == expected["total_trips"]
        assert got["period"] == expected["period"]


@pytest.mark.asyncio
async def test_db_values_win_over_raw_facts():
    row = _row(
        RAW_FACTS,
        total_gross=999,
        total_net=888,
        total_trips=7,
        weekly_gross=(111, 0, 0, 0, 0),
    )
    dossier = await query_submission_dossier("VN-LEGACY", db_pool=_Pool(row))

    assert dossier["gross_income"] == 999
    assert dossier["net_income"] == 888
    assert dossier["trips_count"] == 7
    assert dossier["weekly_incomes"]["week_1"]["gross_revenue"] == 111
    assert dossier["weekly_incomes"]["week_1"]["net_income"] == 1243078
    assert dossier["weekly_incomes"]["week_1"]["total_trips"] == 61
    # Weeks with zero DB columns still recover from raw facts.
    assert dossier["weekly_incomes"]["week_2"]["gross_revenue"] == 4659500


@pytest.mark.asyncio
async def test_dev_record_recovers_income_from_ai_result(monkeypatch, tmp_path):
    record = {
        "submission_id": "DEV-LEGACY",
        "status": "APPROVED",
        "total_gross_revenue": 0,
        "total_net_income": 0,
        "total_trips": 0,
        "weekly_incomes": {},
        "ai_result": json.loads(json.dumps(RAW_FACTS)),
    }
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "dev_submissions.json").write_text(json.dumps([record]), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    dossier = await query_submission_dossier("DEV-LEGACY", db_pool=None)

    assert dossier["gross_income"] == 22621000
    assert dossier["net_income"] == 5355529
    assert dossier["trips_count"] == 253
    assert dossier["weekly_incomes"]["week_5"]["gross_revenue"] == 6014500
