# -*- coding: utf-8 -*-
"""Regression tests for the persisted-forms seed policy.

An empty deployment seeds the built-in default schema; a deployment whose
forms directory already holds JSONs must never resurrect it.
"""

import os

from core.schema_sync import sync_all_persisted_forms
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA


def test_empty_dir_seeds_default_schema(tmp_path):
    registry = sync_all_persisted_forms(str(tmp_path))

    assert DEFAULT_TRUST_BRIDGE_SCHEMA.form_id in registry
    assert os.path.exists(tmp_path / f"{DEFAULT_TRUST_BRIDGE_SCHEMA.form_id}.json")


def test_non_empty_dir_does_not_resurrect_default_schema(tmp_path):
    other = DEFAULT_TRUST_BRIDGE_SCHEMA.model_copy(deep=True)
    other.form_id = "TB_GSM_2026_Nov_Test"
    (tmp_path / f"{other.form_id}.json").write_text(
        other.model_dump_json(indent=2), encoding="utf-8"
    )

    registry = sync_all_persisted_forms(str(tmp_path))

    assert set(registry) == {"TB_GSM_2026_Nov_Test"}
    assert not (tmp_path / f"{DEFAULT_TRUST_BRIDGE_SCHEMA.form_id}.json").exists()
