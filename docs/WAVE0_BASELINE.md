# Wave 0 baseline and PostgreSQL harness

Recorded before Wave 0 harness/CI edits, on revision `8e83727` plus the
pre-existing working-tree docs and `tests/test_ctv_platform.py` comment. No
runtime behavior or assertions were changed.

## Baseline command

```bash
make test
```

which runs:

```bash
.venv/bin/python -m pytest tests -q
```

## Baseline result

```text
39 failed, 541 passed, 1 skipped, 5 warnings in 239.38s (0:03:59)
```

Exit status: `2` (`make` propagated pytest failure). Full output was captured in
`/tmp/wave0_baseline_pytest.txt` during Wave 0; the list below is the durable
record.

### Baseline failures by area

| Area | Count | Implications |
|---|---:|---|
| adversarial form validation / client bypass | 7 | Existing tests already expect stricter server behavior than current implementation; Wave 1/2 must preserve these as acceptance expectations, not weaken them. |
| image/OCR and duplicate decisions | 5 | Existing image checks disagree with expected rejection outcomes; later waves must not treat these as new regressions. |
| Telegram/admin/status workflows | 9 | Existing command dispatch, audit, callback, and webhook-auth tests fail; later waves must not “fix” them by loosening assertions. |
| history/privacy | 1 | Public history endpoint currently returns 200 where the privacy test expects 401; this is the Wave 2 decision point. |
| layout/static routing | 2 | Existing HTML/layout expectations are stale relative to current fixtures; do not infer production behavior from them. |
| webhook/security boundaries | 5 | Existing 400/403/422 expectations fail with 200/500/400 variants; preserve as security acceptance gaps. |
| concurrency/cache/validation | 3 | Existing cache and validation probes fail; they remain baseline failures. |
| schema/path traversal | 3 | Existing schema and sample-path probes fail; they remain baseline failures. |
| geolocation | 2 | Existing cache/header tests fail; unrelated to Wave 0. |
| other architect/image tests | 2 | Existing concurrency and duplicate expectations fail; unrelated to Wave 0. |

### Exact failing test IDs

```text
tests/test_adversarial_round2.py::TestExtremeFinancialEdgeCases::test_income_sanitizer_negative_platform_discount_bypass
tests/test_adversarial_round3.py::TestFormSchemaPathTraversal::test_upload_schema_path_traversal_parent_escape
tests/test_adversarial_round3.py::TestFormSchemaPathTraversal::test_upload_schema_deep_path_traversal_relative_parent
tests/test_adversarial_round3.py::TestWebRTCClientSideValidationBypass::test_webrtc_rejects_numeric_range_violations
tests/test_adversarial_round3.py::TestWebRTCClientSideValidationBypass::test_webrtc_rejects_driver_phone_regex_violations
tests/test_adversarial_round3.py::TestWebRTCClientSideValidationBypass::test_webrtc_rejects_missing_mandatory_signature_and_consent
tests/test_adversarial_round3.py::TestWebRTCClientSideValidationBypass::test_schema_engine_contrast_proves_missing_server_validation
tests/test_architect_fixes.py::TestArchitectFixes::test_parallel_image_checking_concurrency
tests/test_architect_fixes.py::TestArchitectFixes::test_inter_submission_detects_identical_financial_data
tests/test_geo_ip_fallback.py::test_geo_ip_fallback_cloudflare_headers
tests/test_geo_ip_fallback.py::test_geo_ip_fallback_redis_cache_hit
tests/test_image_checking.py::test_endpoint_catches_arithmetic_mismatch_in_ocr
tests/test_image_checking.py::test_monthly_income_upload_rejected_in_weekly_slot
tests/test_image_suite.py::test_admin_query_submission_dossier
tests/test_image_suite.py::test_reject_unauthorized_app_spoofing
tests/test_image_suite.py::test_reject_intra_submission_duplicates_same_screen
tests/test_layout_components.py::test_ctv_landing_page_layout_has_all_needed_and_only_needed_components
tests/test_privacy_and_tg_bot.py::test_survey_history_unauthorized_returns_401
tests/test_privacy_and_tg_bot.py::test_telegram_polling_audit_command
tests/test_privacy_and_tg_bot.py::test_telegram_polling_callback_queries
tests/test_submissions_viewer.py::test_submissions_viewer_authorized
tests/test_telegram_and_features.py::test_telegram_polling_command_dispatcher_start
tests/test_telegram_and_features.py::test_telegram_polling_command_dispatcher_check_and_query
tests/test_telegram_and_features.py::test_telegram_polling_command_dispatcher_forms
tests/test_two_way_relay.py::test_random_subdomain_routing_to_form
tests/test_wave1_bug_and_legality.py::TestTeam4LegalityAndDecree13Compliance::test_decree13_mandatory_consent_rejection_when_omitted
tests/test_wave1_bug_and_legality.py::TestTeam4LegalityAndDecree13Compliance::test_decree13_explicit_dissent_conditional_flow_flaw
tests/test_wave1_bug_and_legality.py::TestTeam4LegalityAndDecree13Compliance::test_telegram_webhook_unauthorized_chat_rejected_with_403
tests/test_wave2_replication_and_novel_probes.py::TestNovelProbeDataStaticExposureVULNDATA01::test_no_data_static_routes_in_fastapi
tests/test_wave3_advanced_security_and_profiling.py::TestTeam3AdvancedSecurity::test_samples_path_traversal_encoded_dotslash_blocked
tests/test_wave3_advanced_security_and_profiling.py::TestTeam3AdvancedSecurity::test_samples_null_byte_handling
tests/test_wave3_advanced_security_and_profiling.py::TestTeam3AdvancedSecurity::test_telegram_webhook_missing_or_null_chat_object_resilience
tests/test_wave3_advanced_security_and_profiling.py::TestTeam3AdvancedSecurity::test_telegram_html_injection_in_sub_id_argument
tests/test_wave3_advanced_security_and_profiling.py::TestTeam3AdvancedSecurity::test_telegram_oversized_message_bounds_probe
tests/test_wave3_advanced_security_and_profiling.py::TestTeam3AdvancedSecurity::test_schema_upload_form_id_regex_strict_enforcement
tests/test_wave3_exploratory_and_concurrency.py::TestTeam1Regression::test_bounded_ttl_cache_concurrent_read_write_stress
tests/test_wave3_exploratory_and_concurrency.py::TestTeam2ExploratoryAndConcurrency::test_payload_boundary_exponential_income_figure
tests/test_wave3_exploratory_and_concurrency.py::TestTeam2ExploratoryAndConcurrency::test_payload_boundary_nan_string_behavior
tests/test_wave3_exploratory_and_concurrency.py::TestTeam4Decree13LegalityAndConsent::test_consent_dissent_immediate_rejection_no_ai_no_db
```

## Isolated PostgreSQL harness

- `scripts/test_postgres.sh` starts a disposable `pgvector/pgvector:pg16`
  container named `survey-pipeline-wave0-pg` on `127.0.0.1:55432`, database
  `survey_pipeline_test`, password `test_only`.
- `UPGRADE_TEST_DATABASE_URL` is supplied only by that script; the helper
  rejects production hosts, production-looking databases, non-PostgreSQL URLs,
  and databases whose name does not end in `_test`.
- `tests/postgres_harness.py` applies `schema.sql` to the disposable database.
- `tests/test_postgres_harness.py` proves: database name/isolation, pgvector,
  core tables, savepoint/transaction rollback isolation, independent-connection
  unique-key behavior, effective-libpq parameter validation (including
  `host`/`hostaddr`/`dbname` query overrides and `service`/`passfile`), rejection
  of inherited `PGHOST`/`PGHOSTADDR`/service routing environment, explicit-host
  enforcement, and rejection of production-looking DSNs. Tests intercept
  `psycopg.connect` to prove validation happens before a connection boundary.
- `tests/test_postgres_runner.py` proves the runner never pre-deletes a named
  container, refuses collisions, requires a local Docker endpoint by default,
  and only removes the container ID it created.
- The harness uses no application lifecycle, providers, production credentials,
  or network synchronization.

Observed harness result:

```text
14 passed in 0.74s
```

CI now runs the same harness in a `postgres-harness` job with a pgvector service
container, publishing service port `55432` and connecting through
`127.0.0.1:55432` from the host runner. Automatic staging/production deploys are
gated to manual `workflow_dispatch` and require both the test job and the
harness job. The destructive mutable-data deployment path remains a later-wave
prerequisite.
